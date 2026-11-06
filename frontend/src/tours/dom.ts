// Finding the element a tour card points at.
import type { Box } from './position.ts';

/** The on-screen element carrying `data-tour="<name>"`, or null. One that is
 *  not laid out (display: none, a collapsed panel), not visible (visibility:
 *  hidden), or inside something hidden from assistive tech (the sidebar while
 *  a live L10 hides it) counts as not there, so its card falls back to the
 *  middle of the screen. An element that is itself aria-hidden (a decorative
 *  icon) still counts: only what it sits inside matters. */
export function findTarget(name: string): HTMLElement | null {
    const selector = `[data-tour="${CSS.escape(name)}"]`;
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(selector))) {
        if (el.parentElement?.closest('[aria-hidden="true"], [inert]') || el.closest('[inert]')) continue;
        if (getComputedStyle(el).visibility === 'hidden') continue;
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return el;
    }
    return null;
}

export function boxOf(el: Element): Box {
    const r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, width: r.width, height: r.height };
}

export function sameBox(a: Box | null, b: Box | null): boolean {
    if (a === b) return true;
    if (!a || !b) return false;
    return (
        Math.abs(a.top - b.top) < 0.5 &&
        Math.abs(a.left - b.left) < 0.5 &&
        Math.abs(a.width - b.width) < 0.5 &&
        Math.abs(a.height - b.height) < 0.5
    );
}
