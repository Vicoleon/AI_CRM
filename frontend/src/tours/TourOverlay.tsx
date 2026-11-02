// The tour on screen: the page dimmed, the element a card talks about lit up,
// and the card beside it with Back / Next / Done and "Skip tour".
//
// Portalled to <body> above everything but toasts. While it is open the page
// underneath takes no clicks (a click there does nothing, so nobody loses a
// tour by brushing the mouse) and no keyboard focus: focus stays on the card,
// or on the overlay itself while the next card is being placed. Escape skips,
// the arrow keys step. The target is re-measured every frame while the card
// is up, which keeps the light on it through smooth scrolling, a resized
// window and a page that is still loading.
import React, { useEffect, useEffectEvent, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { motion, useReducedMotion } from 'framer-motion';
import { Compass, Sparkles, X } from 'lucide-react';
import { boxOf, findTarget, sameBox } from './dom.ts';
import type { TourRun } from './logic.ts';
import {
    DOCK_BELOW,
    MARGIN,
    dockSide,
    fullyVisible,
    placeCard,
    spotlightBox,
    type Box,
    type Size,
} from './position.ts';

/** How long a card waits for its target to appear before sitting centred. */
const STEP_TARGET_WAIT_MS = 1500;
const CARD_WIDTH = 360;
const FOCUSABLE = 'button:not([disabled]), a[href]';

interface Props {
    run: TourRun;
    index: number;
    onIndex: (index: number) => void;
    onFinish: () => void;
    onSkip: () => void;
    onOpenHelp: (topic: string) => void;
    /** Where focus goes when the tour ends (what had it when the tour began). */
    returnFocus: HTMLElement | null;
}

/** The target of the card on screen, and on a phone the edge the card docks
 *  to, chosen once so the card does not jump while the page scrolls. */
type Resolved = { stepKey: string; el: HTMLElement | null; dock: 'dock-top' | 'dock-bottom' };

function viewportSize(): Size {
    return { width: window.innerWidth, height: window.innerHeight };
}

export const TourOverlay: React.FC<Props> = ({ run, index, onIndex, onFinish, onSkip, onOpenHelp, returnFocus }) => {
    const step = run.steps[Math.min(index, run.steps.length - 1)];
    const total = run.steps.length;
    const isFirst = index === 0;
    const isLast = index >= total - 1;
    const stepKey = `${run.key}:${index}:${step.id}`;
    const reduceMotion = useReducedMotion();

    const rootRef = useRef<HTMLDivElement>(null);
    const cardRef = useRef<HTMLDivElement>(null);
    const primaryRef = useRef<HTMLButtonElement>(null);
    const [resolved, setResolved] = useState<Resolved | null>(null);
    const [box, setBox] = useState<Box | null>(null);
    const [viewport, setViewport] = useState<Size>(viewportSize);
    const [cardSize, setCardSize] = useState<Size>({ width: CARD_WIDTH, height: 220 });

    const current = resolved?.stepKey === stepKey ? resolved : null;
    // undefined while still looking; null when the card has no target on screen.
    const target = current ? current.el : undefined;
    const looking = current === null;

    // Find this card's target, giving a page that is still rendering a moment.
    useEffect(() => {
        const name = step.target;
        const started = performance.now();
        let timer = 0;
        const tick = () => {
            const el = name ? findTarget(name) : null;
            if (el || !name || performance.now() - started >= STEP_TARGET_WAIT_MS) {
                const vp = viewportSize();
                const phone = vp.width < DOCK_BELOW;
                let dock = dockSide(el ? boxOf(el) : null, vp);
                if (el && !fullyVisible(boxOf(el), vp)) {
                    // 'instant', not 'auto': the page scroller is scroll-smooth,
                    // and 'auto' would follow it past a reduced-motion setting.
                    // On a phone the target goes to the top and the card docks
                    // below it, so the card never covers what it describes.
                    el.scrollIntoView({
                        block: phone ? 'start' : 'center',
                        inline: 'nearest',
                        behavior: reduceMotion ? 'instant' : 'smooth',
                    });
                    if (phone) dock = 'dock-bottom';
                }
                setResolved({ stepKey, el, dock });
                return;
            }
            timer = window.setTimeout(tick, 100);
        };
        timer = window.setTimeout(tick, 0);
        return () => window.clearTimeout(timer);
    }, [stepKey, step.target, reduceMotion]);

    // Follow the target (and the window) every frame while the card is up. The
    // first frame for a card always writes, so a card with no target drops the
    // previous card's light instead of sitting beside it; nothing is written
    // while the next target is still being looked for (the card is hidden
    // then), so two lit cards in a row glide rather than flash.
    useEffect(() => {
        let frame = 0;
        let last: Box | null | undefined = undefined;
        let lastVp = viewportSize();
        const loop = () => {
            if (target !== undefined) {
                const next = target ? boxOf(target) : null;
                if (last === undefined || !sameBox(next, last)) {
                    last = next;
                    setBox(next);
                }
            }
            const vp = viewportSize();
            if (vp.width !== lastVp.width || vp.height !== lastVp.height) {
                lastVp = vp;
                setViewport(vp);
            }
            frame = window.requestAnimationFrame(loop);
        };
        frame = window.requestAnimationFrame(loop);
        return () => window.cancelAnimationFrame(frame);
    }, [target]);

    // The card's own size, for placing it. Each card is a new element.
    useLayoutEffect(() => {
        const card = cardRef.current;
        if (!card) return;
        const measure = () => {
            const r = card.getBoundingClientRect();
            setCardSize(prev =>
                Math.abs(prev.width - r.width) < 0.5 && Math.abs(prev.height - r.height) < 0.5
                    ? prev
                    : { width: r.width, height: r.height },
            );
        };
        measure();
        const ro = new ResizeObserver(measure);
        ro.observe(card);
        return () => ro.disconnect();
    }, [stepKey]);

    // Focus: on the overlay itself the moment a card changes (the old card and
    // its Next button are gone, and the new one is still hidden while it is
    // placed), then on the new card's main button. Never on the page beneath.
    useLayoutEffect(() => {
        if (looking) rootRef.current?.focus({ preventScroll: true });
        else primaryRef.current?.focus({ preventScroll: true });
    }, [stepKey, looking]);

    // Back where it was when the tour ends. A menu row that started the tour
    // is gone by then, so fall back to the account menu that held it.
    useEffect(() => {
        const before = returnFocus;
        return () => {
            const to =
                before && before.isConnected && before !== document.body
                    ? before
                    : document.querySelector<HTMLElement>('[data-tour="account-menu"]');
            to?.focus({ preventScroll: true });
        };
    }, [returnFocus]);

    const next = () => (isLast ? onFinish() : onIndex(index + 1));
    const back = () => {
        if (!isFirst) onIndex(index - 1);
    };

    const focusables = (): HTMLElement[] =>
        Array.from(cardRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);

    // Keys, caught on the document in the capture phase so they work wherever
    // focus is (each card is a new element, and a key pressed while focus moves
    // to it would otherwise be lost) and the page's own handlers do not also
    // fire. Listening on the document, not the window, lets a window-level
    // layer that is open over the page (the voice panel) take its Escape
    // first; one it has handled is left alone.
    const onKey = useEffectEvent((e: KeyboardEvent) => {
        if (e.defaultPrevented) return;
        if (e.key === 'Tab') {
            const items = focusables();
            if (items.length === 0) return;
            const active = document.activeElement;
            const inCard = !!active && !!cardRef.current?.contains(active) && active !== cardRef.current;
            const first = items[0];
            const lastItem = items[items.length - 1];
            if (!inCard) {
                e.preventDefault();
                (e.shiftKey ? lastItem : first).focus();
            } else if (e.shiftKey && active === first) {
                e.preventDefault();
                lastItem.focus();
            } else if (!e.shiftKey && active === lastItem) {
                e.preventDefault();
                first.focus();
            }
            return;
        }
        const action = e.key === 'Escape' ? onSkip : e.key === 'ArrowRight' ? next : e.key === 'ArrowLeft' ? back : null;
        if (!action || e.repeat) return;
        e.preventDefault();
        e.stopPropagation();
        action();
    });
    useEffect(() => {
        const listener = (e: KeyboardEvent) => onKey(e);
        document.addEventListener('keydown', listener, true);
        return () => document.removeEventListener('keydown', listener, true);
    }, []);

    const light = box ? spotlightBox(box, viewport) : null;
    const pos = placeCard(light, cardSize, viewport, step.placement, current?.dock);
    const docked = pos.placement === 'dock-top' || pos.placement === 'dock-bottom';
    const titleId = `tour-title-${run.key}`;
    const bodyId = `tour-body-${run.key}`;
    const counterId = `tour-counter-${run.key}`;
    const single = total === 1;
    const glide = reduceMotion ? 'none' : 'top 220ms ease, left 220ms ease, width 220ms ease, height 220ms ease';

    return createPortal(
        <div ref={rootRef} tabIndex={-1} className="fixed inset-0 z-[1000] outline-none" data-tour-overlay="">
            {/* The page underneath takes no clicks while a card is up, and a
                click there does not pull focus off the card. */}
            <div
                className="absolute inset-0"
                aria-hidden="true"
                onMouseDown={e => {
                    e.preventDefault();
                    primaryRef.current?.focus({ preventScroll: true });
                }}
            />
            {light ? (
                <div
                    aria-hidden="true"
                    className="pointer-events-none fixed rounded-lg ring-2 ring-white/90"
                    style={{
                        top: light.top,
                        left: light.left,
                        width: light.width,
                        height: light.height,
                        boxShadow: '0 0 0 9999px rgba(17, 24, 39, 0.55)',
                        // Box shadows vanish in Windows high-contrast mode; a
                        // transparent outline turns into a visible frame there.
                        outline: '2px solid transparent',
                        outlineOffset: 2,
                        transition: glide,
                    }}
                />
            ) : (
                <div aria-hidden="true" className="pointer-events-none absolute inset-0 bg-gray-900/55" />
            )}

            <motion.div
                key={stepKey}
                ref={cardRef}
                role="dialog"
                aria-modal="true"
                aria-labelledby={titleId}
                aria-describedby={single ? bodyId : `${counterId} ${bodyId}`}
                tabIndex={-1}
                initial={reduceMotion ? false : { opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: reduceMotion ? 0 : 0.18 }}
                className="fixed flex flex-col overflow-hidden rounded-xl border border-transparent bg-white shadow-2xl ring-1 ring-black/5 outline-none"
                style={{
                    top: pos.top,
                    left: pos.left,
                    width: docked ? viewport.width - 2 * MARGIN : CARD_WIDTH,
                    maxWidth: `calc(100vw - ${2 * MARGIN}px)`,
                    // At high zoom or on a short screen the text scrolls and the
                    // buttons stay on screen.
                    maxHeight: viewport.height - 2 * MARGIN,
                    visibility: looking ? 'hidden' : 'visible',
                }}
            >
                {!single && (
                    <div className="h-1 flex-shrink-0 bg-brand-tint" aria-hidden="true">
                        <div
                            className="h-1 bg-brand"
                            style={{
                                width: `${((index + 1) / total) * 100}%`,
                                transition: reduceMotion ? 'none' : 'width 200ms ease',
                            }}
                        />
                    </div>
                )}
                <div className="min-h-0 flex-1 overflow-y-auto px-5 pt-4 pb-4">
                    <div className="flex items-start justify-between gap-3">
                        <p className="flex min-w-0 items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-brand">
                            {run.kind === 'new' ? (
                                <>
                                    <span className="inline-flex items-center gap-1 rounded-full bg-brand-tint px-2 py-0.5">
                                        <Sparkles size={12} aria-hidden="true" />
                                        New
                                    </span>
                                    <span className="truncate">{run.title}</span>
                                </>
                            ) : (
                                <>
                                    <Compass size={13} aria-hidden="true" className="flex-shrink-0" />
                                    <span className="truncate">{run.title}</span>
                                </>
                            )}
                        </p>
                        <button
                            type="button"
                            onClick={onSkip}
                            aria-label="Skip tour"
                            title="Skip tour"
                            className="-mr-1.5 -mt-1 flex-shrink-0 rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                        >
                            <X size={16} aria-hidden="true" />
                        </button>
                    </div>
                    <h2 id={titleId} className="mt-2 text-base font-semibold text-gray-900">
                        {step.title}
                    </h2>
                    <p id={bodyId} className="mt-1.5 text-sm leading-relaxed text-gray-600">
                        {step.body}
                    </p>
                    {isLast && run.helpTopic && (
                        <a
                            href={`/help/${run.helpTopic}`}
                            onClick={e => {
                                e.preventDefault();
                                onOpenHelp(run.helpTopic!);
                            }}
                            className="mt-3 inline-block rounded text-sm font-medium text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                        >
                            Read the full guide
                        </a>
                    )}
                </div>
                <div className="flex flex-shrink-0 items-center justify-between gap-3 border-t border-gray-100 bg-gray-50 px-5 py-3">
                    {single ? (
                        <span />
                    ) : (
                        <button
                            type="button"
                            onClick={onSkip}
                            className="rounded text-sm text-gray-600 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                        >
                            Skip tour
                        </button>
                    )}
                    <div className="flex items-center gap-2">
                        {!single && (
                            <span id={counterId} className="mr-1 text-xs tabular-nums text-gray-600">
                                <span className="sr-only">{run.title}, step </span>
                                {index + 1} of {total}
                            </span>
                        )}
                        {!isFirst && (
                            <button
                                type="button"
                                onClick={back}
                                className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
                            >
                                Back
                            </button>
                        )}
                        <button
                            ref={primaryRef}
                            type="button"
                            onClick={next}
                            className="rounded-md bg-brand px-3.5 py-1.5 text-sm font-medium text-white hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand focus-visible:ring-offset-2"
                        >
                            {isLast ? (single ? 'Got it' : 'Done') : 'Next'}
                        </button>
                    </div>
                </div>
            </motion.div>
        </div>,
        document.body,
    );
};
