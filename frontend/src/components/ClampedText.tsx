import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Tooltip } from './Tooltip';

/**
 * Static class per line count — Tailwind only emits utilities it can see as
 * literals in the source, so `line-clamp-${lines}` would compile to nothing.
 */
const CLAMP: Record<number, string> = {
    1: 'line-clamp-1',
    2: 'line-clamp-2',
    3: 'line-clamp-3',
};

/**
 * Text clamped to `lines` lines, with the full string available on hover or
 * keyboard focus — but only when it is actually cut off.
 *
 * Two rules make this behave the way a reader expects in a dense list:
 *
 *  1. Clamping to two lines rather than one. Long names (EOS Rocks are written
 *     "Noun-Verb-Date", so they run long by design) mostly fit in full once
 *     they're allowed to wrap, and the row only grows for the ones that need it.
 *  2. The tooltip is conditional on measurement. `-webkit-line-clamp` leaves no
 *     trace in the DOM when it bites, so the only way to know whether the reader
 *     can see everything is to compare scroll height against client height.
 *     Attaching the bubble unconditionally would repeat text that's already on
 *     screen and — since {@link Tooltip} makes its trigger focusable — put every
 *     row in the tab order to reveal nothing.
 *
 * Styling comes from the parent by inheritance; `className` is for the caller's
 * layout (`flex-1 min-w-0` and friends), not for type.
 */
export const ClampedText: React.FC<{
    text: string;
    /** Lines to allow before clamping. Keys of {@link CLAMP} only. */
    lines?: 1 | 2 | 3;
    className?: string;
}> = ({ text, lines = 2, className = '' }) => {
    // Two refs on purpose. `textRef` is the element being clamped, but it
    // remounts whenever the tooltip wrapper appears or disappears around it, so
    // an observer bound to it would go stale after the first toggle and stop
    // noticing later resizes. `boxRef` is the outer span, which never moves.
    const boxRef = useRef<HTMLSpanElement>(null);
    const textRef = useRef<HTMLSpanElement>(null);
    const [clipped, setClipped] = useState(false);

    useLayoutEffect(() => {
        const box = boxRef.current;
        if (!box) return;
        // Sub-pixel line heights make an unclipped element miss by a fraction,
        // so require a full pixel of overflow before calling it cut.
        const measure = () => {
            const el = textRef.current;
            if (el) setClipped(el.scrollHeight - el.clientHeight > 1);
        };
        measure();
        // Re-measure when the column resizes — the same string is whole at one
        // width and cut at another (the L10 panel, wedged between the app nav
        // and the meeting sidebar, gives this cell about half the room the
        // standalone page does).
        const observer = new ResizeObserver(measure);
        observer.observe(box);
        return () => observer.disconnect();
    }, [text, lines]);

    // A late webfont swap changes how much text fits without changing the
    // observed box, so the ResizeObserver above never hears about it.
    useEffect(() => {
        if (!document.fonts) return;
        let live = true;
        document.fonts.ready.then(() => {
            const el = textRef.current;
            if (live && el) setClipped(el.scrollHeight - el.clientHeight > 1);
        });
        return () => {
            live = false;
        };
    }, [text, lines]);

    return (
        // Stable outer box: the Tooltip wrapper comes and goes with `clipped`,
        // and without something constant around it the flex geometry would
        // shift as it mounts — which could un-clip the text, unmount the
        // tooltip, re-clip it, and oscillate.
        <span ref={boxRef} className={`block min-w-0 ${className}`}>
            <Tooltip content={clipped ? text : null} className="max-w-full cursor-help">
                <span ref={textRef} className={`min-w-0 ${CLAMP[lines]}`}>
                    {text}
                </span>
            </Tooltip>
        </span>
    );
};
