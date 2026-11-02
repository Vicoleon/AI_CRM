// Where a tour card goes. Pure geometry so Node's test runner covers it
// (tours.test.ts); TourOverlay.tsx measures the target and the card and asks.
//
// Beside the target on the preferred side, else the opposite side, else the
// other two, always kept inside the screen. A target too big for any side (a
// whole panel) gets the card in the screen's lower-right corner, over it. With
// no target, the middle of the screen. On a phone-width screen the card docks
// to the bottom (or the top, when the target sits in the lower half) at full
// width, because a 360px card beside anything does not fit.
import type { TourPlacement } from './types.ts';

export interface Box {
    top: number;
    left: number;
    width: number;
    height: number;
}

export interface Size {
    width: number;
    height: number;
}

export type CardPlacement = TourPlacement | 'center' | 'over' | 'dock-top' | 'dock-bottom';

export interface CardPosition {
    top: number;
    left: number;
    placement: CardPlacement;
}

/** Space between the target and the card. */
export const GAP = 14;
/** Space kept between the card and the edge of the screen. */
export const MARGIN = 16;
/** Narrower than this, the card docks instead of sitting beside the target. */
export const DOCK_BELOW = 640;
/** How far the spotlight reaches past the target's edge. */
export const SPOTLIGHT_PAD = 6;

const OPPOSITE: Record<TourPlacement, TourPlacement> = {
    top: 'bottom',
    bottom: 'top',
    left: 'right',
    right: 'left',
};

function clamp(value: number, min: number, max: number): number {
    return max < min ? min : Math.min(Math.max(value, min), max);
}

function order(preferred: TourPlacement | undefined): TourPlacement[] {
    const first = preferred ?? 'bottom';
    const rest: TourPlacement[] = ['bottom', 'top', 'right', 'left'].filter(
        p => p !== first && p !== OPPOSITE[first],
    ) as TourPlacement[];
    return [first, OPPOSITE[first], ...rest];
}

function fits(p: TourPlacement, t: Box, card: Size, vp: Size): boolean {
    switch (p) {
        case 'bottom':
            return vp.height - (t.top + t.height) - GAP - MARGIN >= card.height;
        case 'top':
            return t.top - GAP - MARGIN >= card.height;
        case 'right':
            return vp.width - (t.left + t.width) - GAP - MARGIN >= card.width;
        case 'left':
            return t.left - GAP - MARGIN >= card.width;
    }
}

function besides(p: TourPlacement, t: Box, card: Size, vp: Size): CardPosition {
    const maxLeft = vp.width - card.width - MARGIN;
    const maxTop = vp.height - card.height - MARGIN;
    const centredLeft = clamp(t.left + t.width / 2 - card.width / 2, MARGIN, maxLeft);
    const centredTop = clamp(t.top + t.height / 2 - card.height / 2, MARGIN, maxTop);
    switch (p) {
        case 'bottom':
            return { top: t.top + t.height + GAP, left: centredLeft, placement: p };
        case 'top':
            return { top: t.top - GAP - card.height, left: centredLeft, placement: p };
        case 'right':
            return { top: centredTop, left: t.left + t.width + GAP, placement: p };
        case 'left':
            return { top: centredTop, left: t.left - GAP - card.width, placement: p };
    }
}

/** Which edge a phone-width card docks to, chosen once per card so it does
 *  not jump while the page scrolls: the edge away from the target. */
export function dockSide(target: Box | null, viewport: Size): 'dock-top' | 'dock-bottom' {
    return target !== null && target.top + target.height / 2 > viewport.height / 2 ? 'dock-top' : 'dock-bottom';
}

export function placeCard(
    target: Box | null,
    card: Size,
    viewport: Size,
    preferred?: TourPlacement,
    dock?: 'dock-top' | 'dock-bottom',
): CardPosition {
    if (viewport.width < DOCK_BELOW) {
        const lowerHalf = (dock ?? dockSide(target, viewport)) === 'dock-top';
        return lowerHalf
            ? { top: MARGIN, left: MARGIN, placement: 'dock-top' }
            : {
                  top: Math.max(MARGIN, viewport.height - card.height - MARGIN),
                  left: MARGIN,
                  placement: 'dock-bottom',
              };
    }
    if (!target) {
        return {
            top: Math.max(MARGIN, (viewport.height - card.height) / 2),
            left: Math.max(MARGIN, (viewport.width - card.width) / 2),
            placement: 'center',
        };
    }
    for (const p of order(preferred)) {
        if (fits(p, target, card, viewport)) return besides(p, target, card, viewport);
    }
    return {
        top: clamp(viewport.height - card.height - MARGIN, MARGIN, viewport.height),
        left: clamp(viewport.width - card.width - MARGIN, MARGIN, viewport.width),
        placement: 'over',
    };
}

/** The lit-up box around a target, kept inside the screen. Null when the
 *  target is entirely off screen (the overlay then dims everything). */
export function spotlightBox(target: Box, viewport: Size): Box | null {
    const top = Math.max(0, target.top - SPOTLIGHT_PAD);
    const left = Math.max(0, target.left - SPOTLIGHT_PAD);
    const bottom = Math.min(viewport.height, target.top + target.height + SPOTLIGHT_PAD);
    const right = Math.min(viewport.width, target.left + target.width + SPOTLIGHT_PAD);
    if (bottom <= top || right <= left) return null;
    return { top, left, width: right - left, height: bottom - top };
}

/** True when the whole target is on screen (no scroll needed). */
export function fullyVisible(target: Box, viewport: Size): boolean {
    return (
        target.top >= 0 &&
        target.left >= 0 &&
        target.top + target.height <= viewport.height &&
        target.left + target.width <= viewport.width
    );
}
