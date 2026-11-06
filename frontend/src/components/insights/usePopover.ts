import { useEffect, useId, useRef, useState } from 'react';

/**
 * Open/close state for a small anchored popover (a metric's ⓘ, the department
 * picker). Closes on a click outside `rootRef` and on Escape; on Escape the
 * focus goes back to `triggerRef`, so a keyboard user is never dropped at the
 * top of the page.
 *
 * Every setState here runs inside a DOM event handler, never in an effect
 * body (react-hooks/set-state-in-effect is an error in this repo).
 */
export function usePopover<TRoot extends HTMLElement = HTMLDivElement, TTrigger extends HTMLElement = HTMLButtonElement>(
    { onDismiss }: { onDismiss?: () => void } = {},
) {
    const [open, setOpen] = useState(false);
    const rootRef = useRef<TRoot>(null);
    const triggerRef = useRef<TTrigger>(null);
    const id = useId();
    const dismissRef = useRef(onDismiss);

    useEffect(() => {
        dismissRef.current = onDismiss;
    });

    useEffect(() => {
        if (!open) return;
        const onPointer = (e: MouseEvent | TouchEvent) => {
            if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
                setOpen(false);
                dismissRef.current?.();
            }
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') {
                e.stopPropagation();
                setOpen(false);
                dismissRef.current?.();
                triggerRef.current?.focus();
            }
        };
        document.addEventListener('mousedown', onPointer);
        document.addEventListener('touchstart', onPointer);
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('mousedown', onPointer);
            document.removeEventListener('touchstart', onPointer);
            document.removeEventListener('keydown', onKey);
        };
    }, [open]);

    return {
        open,
        setOpen,
        toggle: () => setOpen(o => !o),
        close: () => setOpen(false),
        rootRef,
        triggerRef,
        /** Stable id for `aria-controls` → the popover panel. */
        panelId: `${id}-panel`,
    };
}
