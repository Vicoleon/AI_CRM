import React, { useEffect, useState } from 'react';
import { Link, Navigate, useLocation } from 'react-router-dom';
import { MessageSquare, Search, X } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { companyNameOrDefault } from '../api/company';
import { AskChip, SupportLine } from './help/kit';
import {
    groupedHelpTopics,
    helpPath,
    legacyHelpHashTarget,
    matchesHelpQuery,
} from './help/helpTopics';
import { HELP_GROUP_ICONS, TOPIC_PAGES } from './help/topicPages';
import { scrollParent } from './help/scroll';
import { useHelpAccess } from './help/useHelpAccess';

// Product how-to questions Chat answers from the Help pages.
const HOW_TO_STARTERS = [
    'How do I add a document to the knowledge base?',
    'What can Chat answer about?',
    'How do I connect Slack to Cubie?',
    'What happens when I mark a priority Done?',
    'Where do I change my working hours in Cubie?',
];

/**
 * The Help home, at /help: what Help covers and where to start.
 *
 * Every topic is its own page now (/help/<id>, see ./help/HelpTopic.tsx),
 * listed in the Help panel in the left rail and in the directory below. The
 * topics, their headings and who may read each come from ./help/helpTopics.ts.
 */
export const Help: React.FC = () => {
    const { user, canAccessFullApp, isAdmin } = useAuth();
    const companyName = companyNameOrDefault(user?.workspace?.company_name);
    const access = useHelpAccess();
    const { hash } = useLocation();
    const [query, setQuery] = useState('');

    // Open at the top. Layout's <main> keeps its scroll offset across routes,
    // so "Help home" clicked from far down a topic page would otherwise open
    // the directory partway down.
    const [root, setRoot] = useState<HTMLDivElement | null>(null);
    useEffect(() => {
        scrollParent(root)?.scrollTo({ top: 0, behavior: 'instant' });
    }, [root]);

    // Chat cited Help as /help#<section> until topics became pages, and those
    // answers are still in people's chat history. Send them to the page.
    const legacy = legacyHelpHashTarget(hash);
    if (legacy) return <Navigate to={legacy} replace />;

    const groups = groupedHelpTopics(access)
        .map(({ group, topics }) => ({ group, topics: topics.filter((t) => matchesHelpQuery(t, query)) }))
        .filter((g) => g.topics.length > 0);

    return (
        <div className="max-w-5xl mx-auto p-8" ref={setRoot}>
            <header data-tour="help-header" className="mb-6">
                <h1 className="text-2xl font-semibold text-gray-900">Help</h1>
                <p className="text-sm text-gray-500 mt-1">
                    Guides and answers for getting the most out of Cubie.
                </p>
            </header>

            <p className="text-[15px] text-gray-700 mb-6 leading-relaxed max-w-3xl">
                Cubie is the AI assistant for {companyName}. It pulls from your connected accounts
                automatically — Gmail, Calendar and Drive come from your Google Workspace login;
                Slack you connect yourself in <span className="font-medium">Settings</span>. Just ask
                in plain language; Cubie figures out where to look. Pick a topic below or from the
                Help panel on the left — each one is a short page of its own.
            </p>

            {/* Ask-instead entry point — Chat answers questions about Cubie
                itself from these pages, and every quoted question in Help is a
                one-click link into Chat.

                Gated on core access because Help is ungated and /chat is not:
                for a reader an admin left without Chat, every chip here would
                silently bounce them away. */}
            {canAccessFullApp && (
                <div data-tour="help-ask" className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
                    <div className="flex items-start gap-3">
                        <span className="h-9 w-9 rounded-lg bg-brand/10 text-brand flex items-center justify-center flex-shrink-0">
                            <MessageSquare size={18} />
                        </span>
                        <div className="min-w-0">
                            <h2 className="text-base font-semibold text-gray-900">
                                Would you rather just ask?
                            </h2>
                            <p className="text-sm text-gray-600 mt-1 leading-relaxed">
                                Chat can answer questions about Cubie itself, straight from these
                                pages — ask in your own words and it replies with a link to the page
                                it used. And anywhere in Help, <span className="font-medium">click a
                                question in quotes</span> to ask it in Chat.
                            </p>
                            <div className="flex flex-wrap gap-2 mt-3">
                                {HOW_TO_STARTERS.map((q) => (
                                    <AskChip key={q} q={q} />
                                ))}
                            </div>
                        </div>
                    </div>
                </div>
            )}

            <div data-tour="help-search" className="relative mb-6 max-w-md">
                <Search
                    size={16}
                    className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none"
                    aria-hidden="true"
                />
                <input
                    type="search"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Find a topic"
                    aria-label="Find a help topic"
                    className="w-full rounded-lg border border-gray-200 bg-white pl-9 pr-9 py-2 text-sm text-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-brand/30 focus:border-brand"
                />
                {query && (
                    <button
                        type="button"
                        onClick={() => setQuery('')}
                        aria-label="Clear search"
                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 rounded"
                    >
                        <X size={14} />
                    </button>
                )}
            </div>

            {groups.length === 0 ? (
                <div className="bg-white rounded-lg border border-gray-200 p-6 text-sm text-gray-600">
                    No topic matches &ldquo;{query}&rdquo;.{' '}
                    {canAccessFullApp ? (
                        <>
                            Try asking in{' '}
                            <Link to="/chat" className="text-brand hover:underline">
                                Chat
                            </Link>{' '}
                            — it searches the words on every page, not just the titles.
                        </>
                    ) : (
                        'Try a shorter word.'
                    )}
                </div>
            ) : (
                <div data-tour="help-topics" className="space-y-8">
                    {groups.map(({ group, topics }) => (
                        <section key={group.key} aria-labelledby={`help-group-${group.key}`}>
                            <h2
                                id={`help-group-${group.key}`}
                                className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500 mb-3"
                            >
                                <span className="text-brand">{HELP_GROUP_ICONS[group.title]}</span>
                                {group.title}
                            </h2>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                {topics.map((topic) => {
                                    const Icon = TOPIC_PAGES[topic.id]?.Icon;
                                    return (
                                        <Link
                                            key={topic.id}
                                            to={helpPath(topic.id)}
                                            className="group flex items-start gap-3 bg-white rounded-lg border border-gray-200 p-4 hover:border-brand hover:shadow-sm transition-colors"
                                        >
                                            <span className="h-8 w-8 rounded-md bg-brand/10 text-brand flex items-center justify-center flex-shrink-0">
                                                {Icon && <Icon size={16} />}
                                            </span>
                                            <span className="min-w-0">
                                                <span className="block text-sm font-semibold text-gray-900 group-hover:text-brand">
                                                    {topic.label}
                                                </span>
                                                <span className="block text-sm text-gray-600 mt-0.5 leading-snug">
                                                    {topic.summary}
                                                </span>
                                            </span>
                                        </Link>
                                    );
                                })}
                            </div>
                        </section>
                    ))}
                </div>
            )}

            <SupportLine isAdmin={isAdmin} className="mt-10" />
        </div>
    );
};
