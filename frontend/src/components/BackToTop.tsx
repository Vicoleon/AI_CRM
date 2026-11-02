import React from 'react';
import { ArrowUp } from 'lucide-react';

/**
 * Floating "Back to top" control for long pages.
 *
 * Two details make it work in this app:
 *  - The window never scrolls — Layout's <main> is the scroll container — so we
 *    listen on (and scroll) the nearest scrollable ancestor, not window.
 *  - It's `sticky`, not `fixed`, so it tracks the right edge of the page's own
 *    content column and stays clear of the RadioPlayer, which owns the
 *    bottom-right corner of the viewport at every width.
 *
 * Render it as the last child of a page's content container.
 */
export const BackToTop: React.FC<{ threshold?: number }> = ({ threshold = 400 }) => {
    const anchorRef = React.useRef<HTMLDivElement>(null);
    const scrollerRef = React.useRef<HTMLElement | null>(null);
    const [visible, setVisible] = React.useState(false);

    React.useEffect(() => {
        let node = anchorRef.current?.parentElement ?? null;
        while (node) {
            const { overflowY } = window.getComputedStyle(node);
            if (overflowY === 'auto' || overflowY === 'scroll') break;
            node = node.parentElement;
        }
        scrollerRef.current = node;

        const target: HTMLElement | Window = node ?? window;
        const onScroll = () => setVisible((node ? node.scrollTop : window.scrollY) > threshold);
        onScroll();
        target.addEventListener('scroll', onScroll, { passive: true });
        return () => target.removeEventListener('scroll', onScroll);
    }, [threshold]);

    const scrollToTop = () => {
        const target: HTMLElement | Window = scrollerRef.current ?? window;
        target.scrollTo({ top: 0, behavior: 'smooth' });
    };

    return (
        <div ref={anchorRef} className="sticky bottom-24 z-10 flex justify-end pointer-events-none">
            <button
                type="button"
                onClick={scrollToTop}
                aria-label="Back to top"
                aria-hidden={!visible}
                tabIndex={visible ? 0 : -1}
                className={`flex items-center gap-1.5 rounded-full border border-brand/20 bg-white px-4 py-2 text-sm font-medium text-brand shadow-lg transition-all duration-200 hover:bg-brand hover:text-white ${
                    visible ? 'pointer-events-auto opacity-100' : 'pointer-events-none translate-y-2 opacity-0'
                }`}
            >
                <ArrowUp size={16} />
                Back to top
            </button>
        </div>
    );
};
