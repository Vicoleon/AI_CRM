import React, { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { AnimatePresence, motion } from 'framer-motion';
import { isTopmost as isTopmostDialog, newDialogId, pop as popDialog, push as pushDialog } from './dialogStack';

// Open dialogs register in the shared stack (./dialogStack, which the What's
// New drawer's full mode joins too). A single Escape must only close the
// TOPMOST dialog (a confirm opened over another modal must not close both),
// and focus management targets whichever dialog is frontmost.
const FOCUSABLE =
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

interface ModalProps {
    isOpen: boolean;
    onClose: () => void;
    title: string;
    children: React.ReactNode;
    /** Tailwind max-width class. Defaults to `max-w-2xl` — EOS dialogs are
     * sized for laptop (13–14") screens, so simple forms get 672px and
     * content-heavy dialogs pass `max-w-3xl`/`max-w-4xl`. */
    maxWidth?: string;
}

/**
 * Generic dialog used by the EOS CRUD pages.
 *
 * - Portals into <body> so it escapes any overflow:hidden parents.
 * - Closes on Escape key, backdrop click, and the X button.
 * - Body scroll is locked while open (matches the app's other dialogs).
 * - Framer-motion fade + scale for the content; fade for the backdrop.
 *
 * Forms live in `children`; the modal doesn't render its own footer so each
 * page controls its own submit/cancel layout.
 */
export const Modal: React.FC<ModalProps> = ({
    isOpen,
    onClose,
    title,
    children,
    maxWidth = 'max-w-2xl',
}) => {
    const panelRef = useRef<HTMLDivElement>(null);
    // Keep the latest onClose in a ref so the main effect can depend only on
    // `isOpen`; re-running it whenever onClose's identity changes would re-steal
    // focus into the dialog on every parent re-render.
    const onCloseRef = useRef(onClose);
    useEffect(() => {
        onCloseRef.current = onClose;
    }, [onClose]);

    // Body-scroll lock + Escape-to-close (topmost dialog only) + focus trap and
    // focus restore. Native confirm()/alert() trapped focus by design; these
    // dialogs must do the same so keyboard/screen-reader users can't Tab into
    // the page behind the backdrop, and Escape can't close a dialog underneath.
    useEffect(() => {
        if (!isOpen) return;
        const id = newDialogId();
        pushDialog(id);
        const isTopmost = () => isTopmostDialog(id);
        const previouslyFocused = document.activeElement as HTMLElement | null;

        const onKey = (e: KeyboardEvent) => {
            if (!isTopmost()) return;
            if (e.key === 'Escape') {
                onCloseRef.current();
                return;
            }
            if (e.key === 'Tab') {
                const panel = panelRef.current;
                if (!panel) return;
                const nodes = panel.querySelectorAll<HTMLElement>(FOCUSABLE);
                if (nodes.length === 0) {
                    e.preventDefault();
                    panel.focus();
                    return;
                }
                const first = nodes[0];
                const last = nodes[nodes.length - 1];
                const active = document.activeElement;
                if (e.shiftKey && (active === first || !panel.contains(active))) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && (active === last || !panel.contains(active))) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };

        const prevOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        document.addEventListener('keydown', onKey);
        // Move focus into the dialog once the portal has mounted (defer a tick).
        // Prefers the first focusable — for a destructive confirm that's Cancel,
        // which is the safe default.
        const focusTimer = window.setTimeout(() => {
            const panel = panelRef.current;
            if (!panel) return;
            const target = panel.querySelector<HTMLElement>(FOCUSABLE) ?? panel;
            target.focus();
        }, 0);

        return () => {
            window.clearTimeout(focusTimer);
            document.body.style.overflow = prevOverflow;
            document.removeEventListener('keydown', onKey);
            popDialog(id);
            // Restore focus to whatever triggered the dialog, if still present.
            if (previouslyFocused && document.contains(previouslyFocused)) {
                previouslyFocused.focus();
            }
        };
    }, [isOpen]);

    if (typeof document === 'undefined') return null;

    return createPortal(
        <AnimatePresence>
            {isOpen && (
                <motion.div
                    key="backdrop"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.12 }}
                    onMouseDown={(e) => {
                        // Only close if the mousedown actually started on the backdrop —
                        // prevents a click-and-drag from the form input that ends outside
                        // from accidentally dismissing the modal.
                        if (e.target === e.currentTarget) onClose();
                    }}
                    className="fixed inset-0 z-[200] bg-black/40 flex items-start justify-center pt-10 px-4"
                >
                    <motion.div
                        key="content"
                        ref={panelRef}
                        tabIndex={-1}
                        initial={{ opacity: 0, scale: 0.96, y: -10 }}
                        animate={{ opacity: 1, scale: 1, y: 0 }}
                        exit={{ opacity: 0, scale: 0.96, y: -10 }}
                        transition={{ duration: 0.14, ease: 'easeOut' }}
                        className={`bg-white rounded-xl shadow-2xl w-full ${maxWidth} max-h-[85vh] flex flex-col overflow-hidden`}
                        role="dialog"
                        aria-modal="true"
                        aria-label={title}
                    >
                        <header className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
                            <h3 className="text-lg font-semibold text-gray-900">{title}</h3>
                            <button
                                type="button"
                                onClick={onClose}
                                className="text-gray-400 hover:text-gray-600 transition-colors p-1 -m-1 rounded"
                                aria-label="Close"
                            >
                                <X size={18} />
                            </button>
                        </header>
                        <div className="px-6 py-5 overflow-y-auto">{children}</div>
                    </motion.div>
                </motion.div>
            )}
        </AnimatePresence>,
        document.body,
    );
};

// ─── Form primitives — app-styled, used by EOS modal forms ───────────────

interface FieldProps {
    label: string;
    children: React.ReactNode;
    htmlFor?: string;
}

export const Field: React.FC<FieldProps> = ({ label, children, htmlFor }) => (
    <label className="block" htmlFor={htmlFor}>
        <span className="block text-sm font-medium text-gray-700 mb-1.5">{label}</span>
        {children}
    </label>
);

export const inputCls =
    'block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none';

export const SubmitButton: React.FC<{
    children: React.ReactNode;
    disabled?: boolean;
    onCancel?: () => void;
}> = ({ children, disabled, onCancel }) => (
    <div className="flex items-center justify-end gap-3 pt-2">
        {onCancel && (
            <button
                type="button"
                onClick={onCancel}
                className="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900 transition-colors"
            >
                Cancel
            </button>
        )}
        <button
            type="submit"
            disabled={disabled}
            className="px-4 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
            {children}
        </button>
    </div>
);
