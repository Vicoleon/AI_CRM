import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/** Bubble width (px). Kept in JS so the viewport clamp uses the same number. */
const WIDTH = 260;
/** Gap (px) between the trigger and the bubble. */
const GAP = 8;
/** Below this distance from the top of the viewport, flip the bubble downwards. */
const FLIP_AT = 140;

interface Coords {
    top: number;
    left: number;
    above: boolean;
}

/**
 * Hover / focus tooltip, rendered into <body> through a portal.
 *
 * This exists because the native `title` attribute is the wrong tool wherever
 * the tooltip is the *only* way to read a piece of content: it needs about a
 * second of a perfectly still cursor before it appears, it can't be opened
 * from the keyboard, and it can't be styled — so users reasonably conclude the
 * content isn't there at all. Portalling with `position: fixed` additionally
 * keeps the bubble clear of `overflow` scrollers and sticky stacking contexts
 * (the scorecard grid is both) that would otherwise clip it.
 *
 * Renders `children` untouched when `content` is empty, so callers can pass a
 * nullable field straight through.
 */
export const Tooltip: React.FC<{
    content: React.ReactNode;
    children: React.ReactNode;
    /** Extra classes for the inline wrapper placed around `children`. */
    className?: string;
}> = ({ content, children, className = '' }) => {
    const [coords, setCoords] = useState<Coords | null>(null);
    const wrapRef = useRef<HTMLSpanElement>(null);
    const id = useId();

    const show = useCallback(() => {
        const el = wrapRef.current;
        if (!el) return;
        const r = el.getBoundingClientRect();
        // Prefer above the trigger; flip below for rows sitting near the top of
        // the viewport (the first scorecard rows do, right under the header).
        const above = r.top >= FLIP_AT;
        setCoords({
            top: above ? r.top - GAP : r.bottom + GAP,
            // Centre on the trigger, then clamp so the bubble can't run off
            // either edge of the viewport.
            left: Math.min(
                Math.max(8, r.left + r.width / 2 - WIDTH / 2),
                Math.max(8, window.innerWidth - WIDTH - 8),
            ),
            above,
        });
    }, []);

    const hide = useCallback(() => setCoords(null), []);

    // A fixed-position bubble drifts away from its trigger the moment anything
    // scrolls (the scorecard grid scrolls horizontally under its sticky
    // columns), so dismiss it rather than chase it.
    useEffect(() => {
        if (!coords) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') hide();
        };
        window.addEventListener('scroll', hide, true);
        window.addEventListener('resize', hide);
        window.addEventListener('keydown', onKey);
        return () => {
            window.removeEventListener('scroll', hide, true);
            window.removeEventListener('resize', hide);
            window.removeEventListener('keydown', onKey);
        };
    }, [coords, hide]);

    if (!content) return <>{children}</>;

    return (
        <span
            ref={wrapRef}
            className={`inline-flex items-center ${className}`}
            tabIndex={0}
            aria-describedby={coords ? id : undefined}
            onMouseEnter={show}
            onMouseLeave={hide}
            onFocus={show}
            onBlur={hide}
        >
            {children}
            {coords &&
                createPortal(
                    <div
                        id={id}
                        role="tooltip"
                        style={{
                            position: 'fixed',
                            top: coords.top,
                            left: coords.left,
                            width: WIDTH,
                            // Anchor the bubble's *bottom* edge to `top` when it
                            // sits above the trigger, so its height doesn't have
                            // to be measured.
                            transform: coords.above ? 'translateY(-100%)' : undefined,
                        }}
                        // Above the modal layer (z-[200]) so tooltips inside a
                        // dialog still read. Non-interactive, so it never steals
                        // the pointer from the trigger underneath.
                        className="z-[300] pointer-events-none rounded-lg bg-gray-900 px-3 py-2 text-xs font-normal leading-relaxed text-white shadow-lg whitespace-pre-line break-words"
                    >
                        {content}
                    </div>,
                    document.body,
                )}
        </span>
    );
};
