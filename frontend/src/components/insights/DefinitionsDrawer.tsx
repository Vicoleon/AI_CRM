import React, { useEffect, useId, useRef } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { BookOpenText, X } from 'lucide-react';
import type { MetricDef } from '../../api/insights';

const DIRECTION: Record<MetricDef['good_direction'], string> = {
    up: 'Higher is better',
    down: 'Lower is better',
    neutral: 'Neither way is better on its own',
};

/**
 * Every metric definition on the page in one place: what each number counts,
 * what it is out of, and which way is good. The same catalog feeds the ⓘ
 * beside each tile, so the two never disagree.
 *
 * A modal side panel: Escape or the backdrop closes it, focus starts on the
 * close button and returns to whatever opened it.
 */
export const DefinitionsDrawer: React.FC<{
    open: boolean;
    onClose: () => void;
    metrics: readonly MetricDef[];
    /** "AI Priorities" — which page these are the definitions for. */
    pageTitle: string;
}> = ({ open, onClose, metrics, pageTitle }) => {
    const headingId = useId();
    const closeRef = useRef<HTMLButtonElement>(null);
    const onCloseRef = useRef(onClose);

    useEffect(() => {
        onCloseRef.current = onClose;
    });

    useEffect(() => {
        if (!open) return;
        const opener = document.activeElement as HTMLElement | null;
        closeRef.current?.focus();
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onCloseRef.current();
        };
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('keydown', onKey);
            opener?.focus?.();
        };
    }, [open]);

    return (
        <AnimatePresence>
            {open && (
                <div className="fixed inset-0 z-[80]">
                    <motion.div
                        className="absolute inset-0 bg-gray-900/30"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        onClick={onClose}
                        aria-hidden="true"
                    />
                    <motion.div
                        role="dialog"
                        aria-modal="true"
                        aria-labelledby={headingId}
                        className="absolute inset-y-0 right-0 flex w-full max-w-md flex-col bg-white shadow-xl"
                        initial={{ x: 32, opacity: 0 }}
                        animate={{ x: 0, opacity: 1 }}
                        exit={{ x: 32, opacity: 0 }}
                        transition={{ duration: 0.18, ease: 'easeOut' }}
                    >
                        <div className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4">
                            <div>
                                <h2 id={headingId} className="flex items-center gap-2 text-base font-semibold text-gray-900">
                                    <BookOpenText size={18} className="text-brand" aria-hidden="true" />
                                    Definitions
                                </h2>
                                <p className="mt-0.5 text-xs text-gray-500">What every number on {pageTitle} means.</p>
                            </div>
                            <button
                                ref={closeRef}
                                type="button"
                                onClick={onClose}
                                aria-label="Close definitions"
                                className="-m-1 rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                            >
                                <X size={18} aria-hidden="true" />
                            </button>
                        </div>
                        <div className="flex-1 overflow-y-auto px-5 py-4">
                            {metrics.length === 0 ? (
                                <p className="text-sm text-gray-500">No definitions for this page yet.</p>
                            ) : (
                                <dl className="space-y-4">
                                    {metrics.map(m => (
                                        <div key={m.id} className="rounded-lg border border-gray-100 bg-gray-50/60 p-3">
                                            <dt className="text-sm font-semibold text-gray-900">{m.label}</dt>
                                            <dd className="mt-1 text-[13px] leading-relaxed text-gray-600">{m.definition}</dd>
                                            {m.denominator && (
                                                <dd className="mt-1.5 text-xs text-gray-500">
                                                    <span className="font-medium text-gray-700">Out of:</span> {m.denominator}
                                                </dd>
                                            )}
                                            <dd className="mt-0.5 text-xs text-gray-500">{DIRECTION[m.good_direction] ?? ''}</dd>
                                        </div>
                                    ))}
                                </dl>
                            )}
                            <p className="mt-6 text-xs leading-relaxed text-gray-500">
                                Every percentage on Insights shows what it is out of, and every change compares the
                                period you picked with the same number of days just before it. Days are counted in
                                the company&apos;s business time zone.
                            </p>
                        </div>
                    </motion.div>
                </div>
            )}
        </AnimatePresence>
    );
};
