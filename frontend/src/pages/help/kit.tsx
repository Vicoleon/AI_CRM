import React from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, CheckCircle2, Lock, MessageSquare } from 'lucide-react';
import { PRODUCT_NAME, SUPPORT_EMAIL, VENDOR_NAME } from '../../brand/brand';
import { helpPath } from './helpTopics';

// The building blocks every Help topic page is written in. They lived at the
// top of the one long Help page; each topic now has a page of its own, so they
// moved here where every page can share them.

type Tone = 'info' | 'warn' | 'ok';

const TONE_CLASSES: Record<Tone, string> = {
    info: 'bg-brand-tint border-brand/20 text-brand-hover',
    warn: 'bg-yellow-50 border-yellow-200 text-yellow-800',
    ok: 'bg-green-50 border-green-200 text-green-800',
};

export const Callout: React.FC<{ tone: Tone; children: React.ReactNode }> = ({ tone, children }) => {
    const Icon = tone === 'warn' ? AlertCircle : tone === 'ok' ? CheckCircle2 : Lock;
    return (
        <div className={`flex items-start gap-2 p-3 rounded-md border text-sm ${TONE_CLASSES[tone]}`}>
            <Icon size={16} className="flex-shrink-0 mt-0.5" />
            <div>{children}</div>
        </div>
    );
};

/**
 * A subheading inside a topic page. The `id` makes it a deep link
 * (`/help/eos#rocks`) and a row in the page's "On this page" list, which the
 * topic page builds by reading these headings back out of the rendered page —
 * so adding one here is all it takes to list it.
 */
export const SubHeading: React.FC<{ id: string; children: React.ReactNode }> = ({ id, children }) => (
    <h3
        id={id}
        data-help-subheading=""
        className="text-base font-semibold text-gray-900 pt-4 scroll-mt-6 first:pt-0"
    >
        {children}
    </h3>
);

/**
 * Where to take a question Help does not answer: people ask their admin, and
 * an admin (`isAdmin`) also gets RubikSoft's support address. The Tips topic
 * says the same in both Help homes.
 */
export const SupportLine: React.FC<{ isAdmin: boolean; className?: string }> = ({ isAdmin, className }) => (
    <p className={`text-xs text-gray-500 text-center ${className ?? ''}`}>
        Questions or something not working right? Reach out to your {PRODUCT_NAME} admin.
        {isAdmin && (
            <>
                {' '}
                <span data-tour="help-support-line">
                    Admins can write to {VENDOR_NAME} support at{' '}
                    <a
                        href={`mailto:${SUPPORT_EMAIL}`}
                        className="rounded-sm text-brand hover:text-brand-hover hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        {SUPPORT_EMAIL}
                    </a>
                    .
                </span>
            </>
        )}
    </p>
);

/** A link to another Help topic, e.g. "see the Smart Teams page". */
export const TopicLink: React.FC<{ id: string; children: React.ReactNode }> = ({ id, children }) => (
    <Link to={helpPath(id)} className="text-brand hover:underline">
        {children}
    </Link>
);

// Every example question in Help is a live one — clicking it opens Chat and
// asks it. Chat reads `?q=` on mount and sends immediately (see Chat.tsx), so a
// reader goes from "can it do this?" to the real answer in one click.
const askHref = (q: string) => `/chat?q=${encodeURIComponent(q)}`;

export const AskLink: React.FC<{ q: string }> = ({ q }) => (
    <Link
        to={askHref(q)}
        title="Ask Cubie this in Chat"
        className="group inline-flex items-start gap-1.5 text-left text-gray-800 hover:text-brand transition-colors"
    >
        <span className="underline decoration-dotted decoration-gray-300 underline-offset-2 group-hover:decoration-brand">
            “{q}”
        </span>
        <MessageSquare
            size={12}
            className="flex-shrink-0 mt-1 text-gray-300 group-hover:text-brand transition-colors"
        />
    </Link>
);

export const AskChip: React.FC<{ q: string }> = ({ q }) => (
    <Link
        to={askHref(q)}
        className="inline-flex items-center gap-1.5 text-xs rounded-full border border-gray-200 bg-gray-50 px-3 py-1.5 text-gray-700 hover:border-brand hover:text-brand hover:bg-white transition-colors"
    >
        <MessageSquare size={12} className="flex-shrink-0" />
        {q}
    </Link>
);

export const QCard: React.FC<{ metric: string; qs: string[] }> = ({ metric, qs }) => (
    <div className="bg-gray-50 rounded-lg border border-gray-200 p-4">
        <div className="text-xs font-semibold uppercase tracking-wide text-brand mb-2">{metric}</div>
        <ul className="space-y-1.5">
            {qs.map((q) => (
                <li key={q} className="text-sm">
                    <AskLink q={q} />
                </li>
            ))}
        </ul>
    </div>
);

export const NotYetRow: React.FC<{ name: string; why: string }> = ({ name, why }) => (
    <div className="flex flex-col sm:flex-row sm:gap-4 py-3 border-b border-gray-100 last:border-0">
        <span className="font-medium text-gray-900 sm:w-56 sm:flex-shrink-0">{name}</span>
        <span className="text-gray-500">{why}</span>
    </div>
);
