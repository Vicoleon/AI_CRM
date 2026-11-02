// Rendering a pair working-relationship guide.
//
// EVERY SENTENCE ON THIS SCREEN COMES FROM THE SERVER.
// ---------------------------------------------------
// `comparison.render_markdown` writes the document; this component gives its
// sections containers and renders the markdown inside them. It does not
// summarise, re-word, re-order or add a sentence of its own about either person.
// The only strings this file contributes are structural labels on section
// headers ("Not assessed", "Cubie looked") plus the accessible names on the
// contents nav, and each of those is a restatement of WHICH authored section
// follows — never a claim about the two people.
//
// THE ONE THING THIS SCREEN MUST NOT DO
// -------------------------------------
// The document holds two different negative results. "Where your stated
// preferences pull in different directions" with no numbered subsections means
// Cubie compared the parts reported for both people and found no collision.
// "What this guide cannot speak to" means Cubie could not look, because a layer
// is missing from one or both profiles. `comparison.REFUSALS_INTRO` exists
// precisely so "a missing section is not read as 'nothing found here'".
//
// So the two get deliberately different chrome:
//
//   * A section Cubie COULD NOT READ is dashed-bordered, slate, carries an
//     `EyeOff` and a "Not assessed" badge, and is never collapsed.
//   * A section Cubie READ and found nothing in carries a `SearchCheck` and an
//     "Cubie looked" badge — which is the server's own phrasing for it.
//
// Neither is green and neither is a tick, because neither is a clean bill of
// health for the relationship.
//
// The refusals and completeness sections are also the two that are always
// expanded. Collapsing them by default would hide the missing answers behind an
// interaction, which is the same failure in a different shape.
import React, { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
    AlertTriangle,
    EyeOff,
    Handshake,
    ListChecks,
    MessageSquareQuote,
    SearchCheck,
    Split,
    Table,
} from 'lucide-react';

import type { PairGuideDocument } from '../../../api/smartTeams';
import { AdvisoryNotice } from '../shared';
import {
    parsePairGuide,
    parsePanel,
    type PairSection,
    type PairSectionKind,
} from './pairGuideSections';

// ── Markdown ─────────────────────────────────────────────────────────────────

/** Server markdown, rendered. `remark-gfm` is what makes the synergy table a
 *  table; the document uses no other GFM feature. */
const Prose: React.FC<{ md: string; className?: string }> = ({ md, className }) => (
    <div
        className={`prose prose-sm max-w-none text-gray-700 prose-headings:text-gray-900 prose-strong:text-gray-900 prose-a:text-brand prose-blockquote:border-l-brand/40 prose-blockquote:not-italic prose-blockquote:text-gray-700 prose-table:text-sm ${className ?? ''}`}
    >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
    </div>
);

// ── Per-section chrome ───────────────────────────────────────────────────────

type Tone = 'plain' | 'looked' | 'unassessed' | 'shortfall';

interface Chrome {
    icon: React.ReactNode;
    /** A structural label for the section, not a claim about the pair. */
    badge: string | null;
    tone: Tone;
}

const TONE_CLASSES: Record<Tone, string> = {
    plain: 'border-gray-200 bg-white',
    // Read, and nothing found. Not green: "nothing found in the parts reported
    // for both of you" is not an all-clear on the relationship.
    looked: 'border-gray-200 bg-gray-50',
    // Could not read. Dashed and slate so it cannot be mistaken for the above
    // at a glance or on a monochrome screen — the border style carries the
    // difference as well as the colour.
    unassessed: 'border-dashed border-slate-300 bg-slate-50',
    shortfall: 'border-amber-200 bg-amber-50',
};

const BADGE_CLASSES: Record<Tone, string> = {
    plain: 'bg-gray-100 text-gray-600',
    looked: 'bg-gray-200 text-gray-700',
    unassessed: 'bg-slate-200 text-slate-700',
    shortfall: 'bg-amber-200 text-amber-900',
};

const chromeFor = (section: PairSection): Chrome => {
    switch (section.kind) {
        case 'shortfall':
            return { icon: <AlertTriangle size={16} />, badge: null, tone: 'shortfall' };
        case 'synergy':
            return section.hasTableRows
                ? { icon: <Table size={16} />, badge: null, tone: 'plain' }
                : {
                      icon: <SearchCheck size={16} />,
                      badge: 'Cubie looked',
                      tone: 'looked',
                  };
        case 'friction':
            return section.hasSubsections
                ? { icon: <Split size={16} />, badge: null, tone: 'plain' }
                : {
                      icon: <SearchCheck size={16} />,
                      badge: 'Cubie looked',
                      tone: 'looked',
                  };
        case 'panel':
            return { icon: <Handshake size={16} />, badge: null, tone: 'plain' };
        case 'cards':
            return { icon: <MessageSquareQuote size={16} />, badge: null, tone: 'plain' };
        case 'refusals':
            return {
                icon: <EyeOff size={16} />,
                badge: 'Not assessed',
                tone: 'unassessed',
            };
        case 'completeness':
            return { icon: <ListChecks size={16} />, badge: null, tone: 'looked' };
        default:
            return { icon: null, badge: null, tone: 'plain' };
    }
};

const sectionAnchor = (index: number): string => `pair-section-${index}`;

// ── The two-column panel ─────────────────────────────────────────────────────

/** "What each of you asks for", as two equal columns.
 *
 *  `comparison.PANEL_DISCLAIMER` — rendered above, verbatim — says the two
 *  columns are the same question asked of each person, are the same length on
 *  purpose, and that neither list is worth more than the other. Identical
 *  styling and an equal-width grid are that sentence in layout form. There is no
 *  ordering claim: the server emits left then right and this renders them in
 *  that order. */
const PanelColumns: React.FC<{ body: string }> = ({ body }) => {
    const parsed = useMemo(() => parsePanel(body), [body]);
    if (!parsed) return <Prose md={body} />;
    return (
        <div>
            {parsed.disclaimer && (
                <p className="mb-4 text-sm leading-relaxed text-gray-600">
                    {parsed.disclaimer}
                </p>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
                {parsed.columns.map(column => (
                    <div
                        key={column.heading}
                        className="rounded-lg border border-gray-200 bg-gray-50 p-4"
                    >
                        <h4 className="mb-2 text-sm font-semibold text-gray-900">
                            {column.heading}
                        </h4>
                        <ul className="space-y-1.5">
                            {column.bullets.map((bullet, index) => (
                                <li
                                    key={index}
                                    className="text-sm leading-relaxed text-gray-700"
                                >
                                    <span className="mr-1.5 text-brand" aria-hidden="true">
                                        •
                                    </span>
                                    {bullet}
                                </li>
                            ))}
                        </ul>
                    </div>
                ))}
            </div>
        </div>
    );
};

// ── The document ─────────────────────────────────────────────────────────────

const Section: React.FC<{ section: PairSection; index: number }> = ({
    section,
    index,
}) => {
    const chrome = chromeFor(section);
    return (
        <section
            id={sectionAnchor(index)}
            aria-labelledby={`${sectionAnchor(index)}-heading`}
            className={`rounded-xl border p-5 ${TONE_CLASSES[chrome.tone]}`}
        >
            <div className="mb-3 flex flex-wrap items-center gap-2">
                {chrome.icon && (
                    <span
                        className={
                            chrome.tone === 'unassessed'
                                ? 'text-slate-500'
                                : chrome.tone === 'shortfall'
                                  ? 'text-amber-600'
                                  : 'text-brand'
                        }
                        aria-hidden="true"
                    >
                        {chrome.icon}
                    </span>
                )}
                <h3
                    id={`${sectionAnchor(index)}-heading`}
                    className="text-base font-semibold text-gray-900"
                >
                    {section.heading}
                </h3>
                {chrome.badge && (
                    <span
                        className={`rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${BADGE_CLASSES[chrome.tone]}`}
                    >
                        {chrome.badge}
                    </span>
                )}
            </div>
            {section.kind === 'panel' ? (
                <PanelColumns body={section.body} />
            ) : (
                <Prose md={section.body} />
            )}
        </section>
    );
};

/** A contents list. Structure only — the labels are the document's own headings.
 *
 *  It is here because the "Not assessed" sections sit low in the document
 *  (`render_markdown` puts refusals and completeness after the cards) and a
 *  reader who stops at the friction sections would never see them. The badges
 *  repeat here so the shape of the answer is visible before any of it is read. */
const Contents: React.FC<{ sections: PairSection[] }> = ({ sections }) => {
    if (sections.length < 2) return null;
    return (
        <nav
            aria-label="Sections of this guide"
            className="rounded-xl border border-gray-200 bg-white p-4"
        >
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                In this guide
            </p>
            <ol className="space-y-1">
                {sections.map((section, index) => {
                    const chrome = chromeFor(section);
                    return (
                        <li key={`${section.heading}-${index}`}>
                            <a
                                href={`#${sectionAnchor(index)}`}
                                className="group inline-flex flex-wrap items-baseline gap-2 rounded text-sm text-gray-700 hover:text-brand focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2"
                            >
                                <span className="group-hover:underline">
                                    {section.heading}
                                </span>
                                {chrome.badge && (
                                    <span
                                        className={`rounded-full px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${BADGE_CLASSES[chrome.tone]}`}
                                    >
                                        {chrome.badge}
                                    </span>
                                )}
                            </a>
                        </li>
                    );
                })}
            </ol>
        </nav>
    );
};

/** Which section kinds carry a "we could not read this" meaning. Used only to
 *  decide whether the contents nav is worth an explanatory line. */
const UNASSESSED_KINDS: PairSectionKind[] = ['refusals'];

export interface PairGuideDocumentViewProps {
    guide: PairGuideDocument;
    /** `PairGuideResponse.advisory_notice` — the response-level copy. Falls back
     *  to the document's own, which carries the same sentence. */
    advisoryNotice?: string;
}

export const PairGuideDocumentView: React.FC<PairGuideDocumentViewProps> = ({
    guide,
    advisoryNotice,
}) => {
    const body = guide.content_md ?? '';
    const parsed = useMemo(() => parsePairGuide(body), [body]);

    // `content_md` is present on the read and on a cached generate, and absent
    // on a generate that enqueued. A ready row with no body is a wire problem,
    // not an empty document, and saying so beats rendering a blank page.
    if (!body.trim()) {
        return (
            <div
                role="alert"
                className="rounded-xl border border-amber-200 bg-amber-50 p-5"
            >
                <p className="text-sm font-medium text-amber-900">
                    Cubie says this guide is ready but did not send its text.
                </p>
                <p className="mt-1 text-xs text-amber-800">
                    Reload the page. If it keeps happening, an admin can look at the
                    document row — it is {guide.id}.
                </p>
            </div>
        );
    }

    const hasUnassessed = parsed.sections.some(section =>
        UNASSESSED_KINDS.includes(section.kind),
    );

    return (
        <article className="space-y-4">
            <header className="rounded-xl border border-gray-200 bg-white p-5">
                <h2 className="text-lg font-semibold text-gray-900">
                    {parsed.title || guide.title}
                </h2>
                {parsed.preamble && <Prose className="mt-3" md={parsed.preamble} />}
            </header>

            <Contents sections={parsed.sections} />

            {hasUnassessed && (
                <p className="px-1 text-xs leading-relaxed text-slate-600">
                    Sections marked <span className="font-semibold">Not assessed</span>{' '}
                    are ones Cubie could not read from the two profiles. They are not
                    sections where it looked and found nothing — that is said in the
                    section that says it.
                </p>
            )}

            {parsed.sections.map((section, index) => (
                <Section
                    key={`${section.heading}-${index}`}
                    section={section}
                    index={index}
                />
            ))}

            {parsed.closing && (
                <div className="rounded-xl border border-brand/25 bg-brand-tint/40 p-5">
                    <Prose md={parsed.closing} />
                </div>
            )}

            <AdvisoryNotice
                text={
                    parsed.footer.replace(/^\*|\*$/g, '').trim() ||
                    advisoryNotice ||
                    guide.advisory_notice
                }
            />
        </article>
    );
};

export default PairGuideDocumentView;
