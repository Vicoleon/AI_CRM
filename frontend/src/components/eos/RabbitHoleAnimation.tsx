/**
 * The rabbit-hole "instigator" visual.
 *
 * Three pieces working together:
 *   1. A CSS-only "hole" — squashed dark ellipse with an inner shadow
 *      so the rim reads as a depression in the ground, not a flat disc.
 *   2. A continuously-jumping Lottie rabbit (rabbit.json — exported
 *      from LottieFiles). The Lottie file owns the up/down hop cycle;
 *      Framer carries the *container* across the screen.
 *   3. A caller chip ("Jordan called a rabbit hole") that fades in
 *      in the final third so it doesn't fight the rabbit visually.
 *
 * The component is intentionally split into:
 *   - Static positioning wrappers (left / bottom / translateX in CSS).
 *   - Inner motion.divs that ONLY animate transforms / opacity.
 * Mixing static and animated transforms on the same element subtly
 * breaks (Framer rewrites the whole `transform` property), so we keep
 * them separated.
 *
 * Everything is also wrapped in an ErrorBoundary so a runtime fault
 * inside Lottie / Framer never blanks the meeting view — worst case
 * the rabbit silently degrades to an emoji fallback.
 */
import React from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import Lottie from 'lottie-react';

import rabbitAnimationData from '../../assets/rabbit.json';

interface RabbitHoleAnimationProps {
    visible: boolean;
    /** Bump to force a fresh animation on back-to-back triggers. */
    runKey?: number;
    /** Email / name of whoever pulled the cord — shown as a chip late
     *  in the animation so it doesn't fight with the rabbit visually. */
    caller?: string | null;
}

const TOTAL_DURATION = 3.0; // seconds — fun without overstaying

// ── Error boundary so a Lottie/Framer fault degrades to emoji fallback,
//    not a blank-screened meeting. We log to console so the failure mode
//    is debuggable, but the rest of the L10 view keeps working.
interface ErrorBoundaryState {
    hadError: boolean;
}
class RabbitErrorBoundary extends React.Component<
    React.PropsWithChildren<{ fallback: React.ReactNode }>,
    ErrorBoundaryState
> {
    state: ErrorBoundaryState = { hadError: false };

    static getDerivedStateFromError(): ErrorBoundaryState {
        return { hadError: true };
    }

    componentDidCatch(error: unknown, info: React.ErrorInfo): void {
        // Surface to console so we can diagnose; intentionally NOT
        // toast/alert — the visual is non-critical UX.
        // eslint-disable-next-line no-console
        console.error('[RabbitHole] animation failed:', error, info);
    }

    render(): React.ReactNode {
        return this.state.hadError ? this.props.fallback : this.props.children;
    }
}

// Bare-minimum fallback so a broken Lottie still nudges the team.
const EmojiFallback: React.FC<{ caller?: string | null }> = ({ caller }) => (
    <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-50 select-none flex items-end justify-center pb-32"
    >
        <div className="text-6xl animate-bounce">🐰</div>
        {caller && (
            <div className="absolute bottom-20 text-xs font-medium text-amber-800 bg-amber-50 border border-amber-200 px-3 py-1.5 rounded-full shadow-sm">
                🐰 {caller} called a rabbit hole
            </div>
        )}
    </div>
);

const RabbitHoleAnimationInner: React.FC<RabbitHoleAnimationProps> = ({
    visible,
    runKey = 0,
    caller,
}) => (
    <AnimatePresence>
        {visible && (
            <motion.div
                key={runKey}
                aria-hidden="true"
                className="pointer-events-none fixed inset-0 z-50 select-none"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.2 }}
            >
                {/* ── Hole ───────────────────────────────────────────────
                    Opens first (within the first ~8% of the timeline),
                    stays open through the rabbit's entrance, peek, and
                    fall, then closes after the rabbit is gone (last ~12%
                    of the timeline). */}
                <div
                    style={{
                        position: 'absolute',
                        left: '50%',
                        bottom: '28vh',
                        transform: 'translateX(-50%)',
                    }}
                >
                    <motion.div
                        style={{
                            width: '200px',
                            height: '50px',
                            background:
                                'radial-gradient(ellipse at center, #050505 0%, #1a1a1a 55%, transparent 75%)',
                            borderRadius: '50%',
                            boxShadow:
                                'inset 0 -10px 16px rgba(0,0,0,0.65), 0 6px 14px rgba(0,0,0,0.15)',
                            transformOrigin: '50% 50%',
                        }}
                        initial={{ opacity: 0, scaleX: 0.2, scaleY: 0.4 }}
                        animate={{
                            // Open early, stay open, close at the very end.
                            opacity: [0, 1, 1, 0],
                            scaleX: [0.2, 1, 1, 0.5],
                            scaleY: [0.4, 1, 1, 0.6],
                        }}
                        transition={{
                            duration: TOTAL_DURATION,
                            // 0% → 8% hole opens
                            // 8% → 88% hole stays open (rabbit entrance + fall)
                            // 88% → 100% hole closes
                            times: [0, 0.08, 0.88, 1],
                            ease: 'easeInOut',
                        }}
                    />
                </div>

                {/* ── Rabbit ─────────────────────────────────────────────
                    Container is 240 wide × 360 tall — matches the JSON's
                    400×600 aspect ratio so the taller canvas (needed for
                    the bigger hop) still renders un-squished.

                    The Lottie itself faces left, but we want the rabbit
                    pointing into the hole as it travels rightward, so we
                    flip the inner wrapper horizontally with scaleX(-1).
                    That inversion lives on its own div so Framer's scale
                    animation on the parent stays clean.

                    Timing-wise the rabbit is INVISIBLE for the first 10%
                    (hole opens alone), then fades in + travels in, peeks,
                    and falls — all done by 82%. After 82% the rabbit is
                    gone so the hole sits empty before closing. */}
                <div
                    style={{
                        position: 'absolute',
                        left: '50%',
                        bottom: '24vh',
                        marginLeft: '-120px',
                        width: '240px',
                        height: '360px',
                    }}
                >
                    <motion.div
                        style={{
                            width: '100%',
                            height: '100%',
                            transformOrigin: '50% 90%',
                        }}
                        initial={{
                            x: '-110vw',
                            y: 0,
                            scale: 1,
                            opacity: 0,
                        }}
                        animate={{
                            opacity: [0, 0, 1, 1, 1, 1, 0, 0],
                            x: [
                                '-110vw',
                                '-110vw',
                                '-100vw',
                                '0vw',
                                '0vw',
                                '0vw',
                                '0vw',
                                '0vw',
                            ],
                            y: [0, 0, 0, 0, 0, 25, 55, 55],
                            scale: [1, 1, 1, 1, 1, 0.5, 0, 0],
                        }}
                        transition={{
                            duration: TOTAL_DURATION,
                            //  0  → 10%  hole opens alone (rabbit invisible)
                            // 10  → 15%  rabbit fades in, still off-screen
                            // 15  → 60%  rabbit hops in from left
                            // 60  → 68%  peek pause over the hole
                            // 68  → 76%  start falling (scale 1 → 0.5)
                            // 76  → 82%  finish falling (scale → 0, gone)
                            // 82  → 100% rabbit fully gone, hole alone
                            times: [0, 0.1, 0.15, 0.6, 0.68, 0.76, 0.82, 1],
                            ease: 'easeInOut',
                        }}
                    >
                        {/* Mirror wrapper — flips the rabbit horizontally so
                            it faces the direction of travel. */}
                        <div
                            style={{
                                width: '100%',
                                height: '100%',
                                transform: 'scaleX(-1)',
                            }}
                        >
                            <Lottie
                                animationData={rabbitAnimationData}
                                loop
                                autoplay
                                style={{ width: '100%', height: '100%' }}
                            />
                        </div>
                    </motion.div>
                </div>

                {/* ── Caller chip ──────────────────────────────────────── */}
                {caller && (
                    <div
                        style={{
                            position: 'absolute',
                            left: '50%',
                            bottom: '14vh',
                            transform: 'translateX(-50%)',
                        }}
                    >
                        <motion.div
                            className="text-xs font-medium text-amber-800 bg-amber-50 border border-amber-200 px-3 py-1.5 rounded-full shadow-sm whitespace-nowrap"
                            initial={{ opacity: 0, y: 10 }}
                            animate={{
                                opacity: [0, 0, 1, 1, 0],
                                y: [10, 10, 0, 0, -4],
                            }}
                            transition={{
                                duration: TOTAL_DURATION,
                                times: [0, 0.62, 0.78, 0.92, 1],
                                ease: 'easeInOut',
                            }}
                        >
                            🐰 {caller} called a rabbit hole
                        </motion.div>
                    </div>
                )}
            </motion.div>
        )}
    </AnimatePresence>
);

export const RabbitHoleAnimation: React.FC<RabbitHoleAnimationProps> = (
    props,
) => {
    // Only mount the fallback while the animation should be visible —
    // otherwise the boundary's children are absent and there's nothing
    // to render either way.
    return (
        <RabbitErrorBoundary
            fallback={props.visible ? <EmojiFallback caller={props.caller} /> : null}
        >
            <RabbitHoleAnimationInner {...props} />
        </RabbitErrorBoundary>
    );
};
