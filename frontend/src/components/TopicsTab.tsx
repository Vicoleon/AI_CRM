import React, { useState, useEffect } from 'react';
import { Plus, Tag, Trash2 } from 'lucide-react';
import client from '../api/client';
import { motion, AnimatePresence } from 'framer-motion';

/**
 * Topic management, rendered as the admin-only "Topics" tab inside the Assets
 * page. (Was the standalone /topics page.) Backend gates create/delete to
 * admins; this tab is also only shown to admins by the Assets page.
 */
export const TopicsTab: React.FC = () => {
    const [topics, setTopics] = useState<string[]>([]);
    const [newTopic, setNewTopic] = useState('');
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        fetchTopics();
    }, []);

    const fetchTopics = async () => {
        try {
            const { data } = await client.get('/api/topics/');
            setTopics(data);
        } catch (err) {
            console.error('Failed to fetch topics:', err);
        }
    };

    const handleAdd = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!newTopic.trim()) return;

        setLoading(true);
        try {
            await client.post('/api/topics/', { topic: newTopic });
            setNewTopic('');
            fetchTopics();
        } catch (err) {
            console.error('Failed to add topic:', err);
        } finally {
            setLoading(false);
        }
    };

    const handleRemove = async (topic: string) => {
        try {
            await client.delete(`/api/topics/${encodeURIComponent(topic)}`);
            fetchTopics();
        } catch (err) {
            console.error('Failed to remove topic:', err);
        }
    };

    return (
        <div>
            <p className="text-[#64748b] text-[15px] mb-6">Manage document categorization topics</p>

            {/* Add Topic Form */}
            <div className="bg-white rounded-[12px] border border-gray-100 p-6 mb-8 w-fit shadow-[0_2px_8px_rgb(0,0,0,0.02)]">
                <form onSubmit={handleAdd} className="flex gap-4 items-center">
                    <input
                        type="text"
                        value={newTopic}
                        onChange={(e) => setNewTopic(e.target.value)}
                        placeholder="Enter new topic name..."
                        className="w-[320px] border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 placeholder-gray-400 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 transition-all font-medium"
                    />
                    <button
                        type="submit"
                        disabled={loading || !newTopic.trim()}
                        className="bg-brand hover:bg-brand-hover text-white text-[14px] font-medium px-5 py-2.5 rounded-[6px] transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                        <Plus strokeWidth={2.5} size={16} />
                        Add Topic
                    </button>
                </form>
            </div>

            {/* Topics Table */}
            <div className="overflow-x-auto bg-white border border-gray-100 rounded-lg">
                <table className="w-full text-left">
                    <thead>
                        <tr className="text-gray-500 text-sm border-b border-gray-100 bg-gray-50/50">
                            <th className="py-3 px-4 font-semibold w-3/4">Topic Name</th>
                            <th className="py-3 px-4 font-semibold text-center w-1/4">Action</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                        <AnimatePresence>
                            {topics.map((topic) => (
                                <motion.tr
                                    key={topic}
                                    initial={{ opacity: 0, y: -10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: 10 }}
                                    className="group hover:bg-gray-50/50 transition-colors"
                                >
                                    <td className="py-3 px-4">
                                        <div className="flex items-center gap-3">
                                            <div className="h-9 w-9 bg-brand-tint rounded-full flex items-center justify-center text-brand text-sm font-bold shadow-sm">
                                                <Tag size={16} strokeWidth={2.5} />
                                            </div>
                                            <div className="flex flex-col">
                                                <span className="font-semibold text-sm text-gray-900">{topic}</span>
                                            </div>
                                        </div>
                                    </td>
                                    <td className="py-3 px-4 text-center">
                                        <button
                                            onClick={() => handleRemove(topic)}
                                            className="text-gray-400 hover:text-red-600 transition-colors p-1.5 rounded hover:bg-red-50"
                                            title="Delete Topic"
                                        >
                                            <Trash2 className="h-4 w-4" />
                                        </button>
                                    </td>
                                </motion.tr>
                            ))}
                        </AnimatePresence>
                    </tbody>
                </table>
            </div>

            {topics.length === 0 && (
                <div className="text-center py-12 text-gray-500 bg-white rounded-xl border border-gray-100 shadow-sm mt-8">
                    <Tag size={40} className="mx-auto mb-3 text-gray-300" strokeWidth={1.5} />
                    <p className="text-[15px]">No topics yet. Add your first topic above.</p>
                </div>
            )}
        </div>
    );
};
