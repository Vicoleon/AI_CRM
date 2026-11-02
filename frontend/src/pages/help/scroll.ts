/**
 * The element a Help page scrolls inside. Layout's <main> is the scroll
 * container — the window never scrolls — and it keeps its offset across route
 * changes, so a Help page that should open at its top has to reset it itself.
 */
export const scrollParent = (el: HTMLElement | null): HTMLElement | null => {
    let node = el?.parentElement ?? null;
    while (node) {
        const { overflowY } = window.getComputedStyle(node);
        if (overflowY === 'auto' || overflowY === 'scroll') return node;
        node = node.parentElement;
    }
    return null;
};
