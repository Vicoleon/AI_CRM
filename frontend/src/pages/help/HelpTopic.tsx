import React, { useCallback, useEffect, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { ArrowLeft, ArrowRight, MessageSquare } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import { BackToTop } from '../../components/BackToTop';
import {
    canReadHelpTopic,
    findHelpTopic,
    helpGroupTitle,
    helpNeighbours,
    helpPath,
} from './helpTopics';
import { SupportLine } from './kit';
import { scrollParent } from './scroll';
import { TOPIC_PAGES } from './topicPages';
import { useHelpAccess } from './useHelpAccess';

/**
 * One Help topic, at /help/:topicId.
 *
 * Help used to be a single 2,700-line page; each topic is now its own page,
 * reached from the Help panel in the left rail. The page frame (heading,
 * "On this page", previous/next) lives here so every topic reads the same way;
 * the words live in ./topics/<Topic>.tsx.
 */
export const HelpTopic: React.FC = () => {
    const { topicId } = useParams<{ topicId: string }>();
    const { hash } = useLocation();
    const { canAccessFullApp, isAdmin, isLoading, token, user } = useAuth();
    const access = useHelpAccess();
    const topic = findHelpTopic(topicId);
    const page = topic ? TOPIC_PAGES[topic.id] : undefined;
    const readable = !!topic && !!page && canReadHelpTopic(topic, access);

    // "On this page", read back out of the rendered article: every SubHeading
    // a topic declares becomes a row, with nothing to keep in step by hand.
    // A callback ref rather than an effect, so the list is read once per topic
    // as the article mounts — keyed on the topic, it re-reads when the reader
    // moves to the next one.
    const [outline, setOutline] = useState<{ id: string; label: string }[]>([]);
    const articleRef = useCallback(
        (el: HTMLElement | null) => {
            if (!el) return;
            const rows = Array.from(el.querySelectorAll<HTMLElement>('[data-help-subheading]')).map((h) => ({
                id: h.id,
                label: h.textContent ?? '',
            }));
            setOutline(rows);
        },
        // eslint-disable-next-line react-hooks/exhaustive-deps -- re-read per topic
        [topicId],
    );

    // A new topic opens at its top. The panel is a list of links, so moving from
    // a long page to the next one would otherwise keep the old scroll offset and
    // land the reader halfway down a page they have not started. A `#heading`
    // in the URL wins: that link was to a place on the page, not to the page.
    const [anchor, setAnchor] = useState<HTMLDivElement | null>(null);
    // The heading is found on the next frame, not now: arriving from another
    // topic (Back, say), this runs before the new "On this page" box has
    // rendered above the article, and measuring then lands the reader off by
    // that box's height.
    useEffect(() => {
        if (!anchor) return;
        const target = hash ? document.getElementById(hash.slice(1)) : null;
        if (target) {
            const frame = requestAnimationFrame(() => target.scrollIntoView({ block: 'start' }));
            return () => cancelAnimationFrame(frame);
        }
        scrollParent(anchor)?.scrollTo({ top: 0, behavior: 'instant' });
    }, [anchor, topicId, hash]);

    // A gated topic can only be judged once /api/users/me has answered.
    // `isLoading` is still false on the first render after a hard load — the
    // fetch starts in AuthContext's effect — so "a token and no profile yet"
    // counts as loading too, as it does in App.tsx's route guards. Ungated
    // topics render straight away, so a failed profile fetch cannot strand them.
    const awaitingProfile = isLoading || (!!token && !user);
    if (topic?.requires && awaitingProfile) {
        return <div className="p-8 text-center text-gray-500">Loading…</div>;
    }

    if (!readable || !topic || !page) {
        return (
            <div className="max-w-3xl mx-auto p-8">
                <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
                    <h1 className="text-lg font-semibold text-gray-900">We couldn&apos;t find that help page</h1>
                    <p className="text-sm text-gray-500 mt-2">
                        It may have moved, or it covers a feature your account doesn&apos;t have yet.
                    </p>
                    <Link
                        to="/help"
                        className="inline-flex items-center gap-1.5 mt-4 text-sm font-medium text-brand hover:underline"
                    >
                        <ArrowLeft size={14} /> All help topics
                    </Link>
                </div>
            </div>
        );
    }

    const { Page, Icon } = page;
    const { prev, next } = helpNeighbours(topic.id, access);

    return (
        <div className="max-w-3xl mx-auto px-6 py-8" ref={setAnchor}>
            <nav aria-label="Breadcrumb" className="text-xs text-gray-500 mb-3">
                <Link to="/help" className="hover:text-brand hover:underline">
                    Help
                </Link>
                <span className="mx-1.5" aria-hidden="true">/</span>
                <span>{helpGroupTitle(topic.group)}</span>
            </nav>

            <header className="mb-6">
                <h1 className="flex items-center gap-2.5 text-2xl font-semibold text-gray-900">
                    <span className="h-9 w-9 rounded-lg bg-brand/10 text-brand flex items-center justify-center flex-shrink-0">
                        <Icon size={19} />
                    </span>
                    {topic.title}
                </h1>
                <p className="text-[15px] text-gray-600 mt-2 leading-relaxed">{topic.summary}</p>
            </header>

            {outline.length > 1 && (
                <nav
                    aria-label="On this page"
                    className="bg-white rounded-lg border border-gray-200 px-5 py-4 mb-6"
                >
                    <div className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">
                        On this page
                    </div>
                    <ul className="grid grid-cols-1 sm:grid-cols-2 gap-x-6">
                        {outline.map((row) => (
                            <li key={row.id}>
                                <a
                                    href={`#${row.id}`}
                                    className="block py-1 text-sm text-brand hover:text-brand-hover hover:underline"
                                >
                                    {row.label}
                                </a>
                            </li>
                        ))}
                    </ul>
                </nav>
            )}

            <article
                key={topic.id}
                ref={articleRef}
                className="bg-white rounded-lg border border-gray-200 p-6 sm:p-8 space-y-4 text-[15px] text-gray-700 leading-7"
            >
                <Page />
            </article>

            {(prev || next) && (
                <nav aria-label="More help topics" className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-6">
                    {prev ? (
                        <Link
                            to={helpPath(prev.id)}
                            className="group bg-white rounded-lg border border-gray-200 px-4 py-3 hover:border-brand transition-colors"
                        >
                            <span className="flex items-center gap-1 text-xs text-gray-500">
                                <ArrowLeft size={12} /> Previous
                            </span>
                            <span className="block text-sm font-medium text-gray-900 group-hover:text-brand">
                                {prev.label}
                            </span>
                        </Link>
                    ) : (
                        <span className="hidden sm:block" />
                    )}
                    {next && (
                        <Link
                            to={helpPath(next.id)}
                            className="group bg-white rounded-lg border border-gray-200 px-4 py-3 text-right hover:border-brand transition-colors"
                        >
                            <span className="flex items-center justify-end gap-1 text-xs text-gray-500">
                                Next <ArrowRight size={12} />
                            </span>
                            <span className="block text-sm font-medium text-gray-900 group-hover:text-brand">
                                {next.label}
                            </span>
                        </Link>
                    )}
                </nav>
            )}

            {/* Gated on the copilot because Help is ungated and /chat is not: an
                EOS-only reader sent there would bounce to the L10 list. */}
            {canAccessFullApp && (
                <p className="flex items-center justify-center gap-1.5 text-sm text-gray-500 mt-8">
                    <MessageSquare size={14} className="text-brand" />
                    Still stuck?{' '}
                    <Link to="/chat" className="text-brand hover:underline">
                        Ask Cubie in Chat
                    </Link>
                    — it answers from these pages.
                </p>
            )}
            <SupportLine isAdmin={isAdmin} className="mt-3" />

            <BackToTop />
        </div>
    );
};
