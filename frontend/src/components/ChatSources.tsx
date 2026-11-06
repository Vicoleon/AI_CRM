import React, { useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { BookMarked, ChevronDown, ChevronRight } from 'lucide-react';

export interface Citation {
    source_path: string;
    page?: number;
    chunk_index: number;
    snippet: string;
}

/** One document, plus every passage the answer pulled from it. */
interface SourceGroup {
    path: string;
    title: string;
    context: string;
    citations: Citation[];
}

/** Documents shown before the reader has to ask for the rest. */
const PREVIEW_COUNT = 3;

/**
 * Human title for a retrieved document.
 *
 * Citations carry only the storage path — `gs://bucket/HR/Employee_Handbook_2025.pdf`
 * — so the file name is all we have to name the document with. Separators become
 * spaces and the extension goes, but casing is left alone: a company's files are
 * full of acronyms (HR, QC, SOW) that any title-casing pass would flatten.
 */
function sourceTitle(path: string): string {
    const bare = path.split(/[?#]/)[0];
    const last = bare.split('/').filter(Boolean).pop() || bare;
    let name = last;
    try {
        name = decodeURIComponent(last);
    } catch {
        // Percent signs that aren't escapes (a literal "100%_target.pdf") throw
        // here — the raw name is already the right answer in that case.
    }
    return name
        .replace(/\.[A-Za-z0-9]{1,5}$/, '')
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim() || name;
}

/**
 * The italic line under the title — where the document lives, so two files
 * with the same name are still tellable apart.
 *
 * Folder segments come first, then the file type. The bucket/host segment is
 * dropped: it is the same string on every card and tells the reader nothing.
 */
function sourceContext(path: string): string {
    const bare = path.split(/[?#]/)[0];
    const parts: string[] = [];

    const schemeMatch = bare.match(/^([a-z][a-z0-9+.-]*):\/\/(.+)$/i);
    const body = schemeMatch ? schemeMatch[2] : bare;
    const segments = body.split('/').filter(Boolean);
    // Drop the container (GCS bucket or web host) and the file itself; what's
    // left is the folder trail a colleague recognizes ("Human Resources").
    const folders = schemeMatch ? segments.slice(1, -1) : segments.slice(0, -1);
    if (folders.length) {
        parts.push(folders.map((f) => f.replace(/[_-]+/g, ' ')).join(' / '));
    }

    const ext = segments[segments.length - 1]?.match(/\.([A-Za-z0-9]{1,5})$/);
    if (ext) parts.push(ext[1].toUpperCase());

    return parts.join(' · ') || 'Knowledge base';
}

/**
 * Collapse a citation list into one entry per document.
 *
 * Retrieval routinely returns three or four chunks of the same handbook. Listing
 * each as its own card reads as "we found many sources" when we found one, so
 * the passages fold into the document card that produced them and the count
 * moves to a badge.
 */
function groupCitations(citations: Citation[]): SourceGroup[] {
    const byPath = new Map<string, SourceGroup>();
    for (const cite of citations) {
        const existing = byPath.get(cite.source_path);
        if (existing) {
            existing.citations.push(cite);
            continue;
        }
        byPath.set(cite.source_path, {
            path: cite.source_path,
            title: sourceTitle(cite.source_path),
            context: sourceContext(cite.source_path),
            citations: [cite],
        });
    }
    return [...byPath.values()];
}

/**
 * The "Sources" panel under an assistant answer: a titled card per cited
 * document, each previewing the passage the answer drew on.
 *
 * Open by default. The evidence behind an answer is the point of a grounded
 * assistant, and a closed drawer labelled "Sources (13)" made citations feel
 * like debug output. Long lists stay short instead: only the first few
 * documents render until the reader asks for the rest.
 */
export const ChatSources: React.FC<{ citations: Citation[] }> = ({ citations }) => {
    const [isOpen, setIsOpen] = useState(true);
    const [showAll, setShowAll] = useState(false);

    const groups = useMemo(() => groupCitations(citations), [citations]);
    const visible = showAll ? groups : groups.slice(0, PREVIEW_COUNT);
    const hidden = groups.length - visible.length;

    return (
        <div className="rounded-xl border border-brand-soft/60 bg-brand-tint/50 p-3">
            <button
                onClick={() => setIsOpen(!isOpen)}
                className="flex w-full items-center justify-between text-left"
                aria-expanded={isOpen}
            >
                <span className="flex items-center gap-2">
                    <BookMarked size={15} className="text-brand" />
                    <span className="text-sm font-semibold text-brand">Sources</span>
                    <span className="rounded-full bg-brand-tint px-2 py-0.5 text-xs font-semibold text-brand tabular-nums">
                        {groups.length}
                    </span>
                </span>
                <span className="text-brand/60">
                    {isOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                </span>
            </button>

            <AnimatePresence initial={false}>
                {isOpen && (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden"
                    >
                        <div className="mt-3 space-y-2">
                            {visible.map((group) => (
                                <SourceCard key={group.path} group={group} />
                            ))}
                        </div>

                        {(hidden > 0 || showAll) && (
                            <button
                                onClick={() => setShowAll(!showAll)}
                                className="mt-2 w-full rounded-lg px-2 py-1.5 text-xs font-semibold text-brand hover:bg-brand-tint transition-colors"
                            >
                                {showAll
                                    ? 'Show fewer sources'
                                    : `Show all ${groups.length} sources`}
                            </button>
                        )}
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
};

/**
 * One document card: title, where it came from, and the passage itself —
 * clamped to two lines until the reader opens it for the full quote.
 */
const SourceCard: React.FC<{ group: SourceGroup }> = ({ group }) => {
    const [expanded, setExpanded] = useState(false);
    const passages = group.citations.length;

    return (
        <div className="rounded-lg border border-brand-soft/60 bg-white transition-colors hover:border-brand-soft">
            <button
                onClick={() => setExpanded(!expanded)}
                className="w-full px-3 py-2.5 text-left"
                aria-expanded={expanded}
            >
                <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-semibold leading-snug text-gray-900">
                        {group.title}
                    </span>
                    <span className="mt-0.5 flex flex-shrink-0 items-center gap-1.5">
                        {passages > 1 && (
                            <span className="rounded-full bg-brand-tint px-1.5 py-0.5 text-[10px] font-semibold text-brand tabular-nums">
                                {passages} passages
                            </span>
                        )}
                        <span className="text-gray-400">
                            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        </span>
                    </span>
                </div>

                <p className="mt-0.5 text-xs italic text-brand/70">{group.context}</p>

                {!expanded && (
                    <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-gray-600">
                        {group.citations[0].snippet}
                    </p>
                )}
            </button>

            {expanded && (
                <div className="space-y-2 border-t border-brand-tint px-3 py-2.5">
                    {group.citations.map((cite, idx) => (
                        <div key={idx} className="border-l-2 border-brand-soft/60 pl-2.5">
                            <p className="text-xs leading-relaxed text-gray-700">
                                “{cite.snippet}”
                            </p>
                            <div className="mt-1 flex gap-2 font-mono text-[10px] text-gray-400">
                                <span>Page: {cite.page ?? 'N/A'}</span>
                                <span>Chunk: {cite.chunk_index}</span>
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};
