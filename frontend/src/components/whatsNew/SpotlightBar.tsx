// The one-line "New since your last visit" bar on the Daily Briefing
// (Homefeed.tsx), directly under the page header.
//
// It names the newest update the person has not seen and how many more there
// are; "See what's new" opens the drawer, × hides the bar until something newer
// is published (memory.ts `barDismissedThrough`). Renders nothing when there is
// nothing unseen past that point, or where What's New is off.
import React from 'react';
import { ChevronRight, Sparkles, X } from 'lucide-react';
import { spotlightText } from './spotlight';
import { openWhatsNew } from './store';
import { useWhatsNew } from './WhatsNewProvider';

export const SpotlightBar: React.FC = () => {
    const { off, barItems, dismissBar } = useWhatsNew();
    const text = off ? null : spotlightText(barItems);
    if (!text) return null;

    return (
        <div className="px-8 mt-6">
            <div
                role="region"
                aria-label="New since your last visit"
                className="flex flex-wrap items-center gap-x-3 rounded-xl border border-brand/20 bg-brand-tint py-0.5 pl-4 pr-0.5 text-brand-hover"
            >
                <Sparkles size={16} className="flex-shrink-0 text-brand" aria-hidden="true" />
                <p className="min-w-0 flex-[1_1_260px] py-3 text-sm leading-5">
                    <span className="font-semibold">New since your last visit:</span> {text}
                </p>
                <div className="flex items-center gap-0.5">
                    <button
                        type="button"
                        onClick={() => openWhatsNew({ from: 'bar' })}
                        aria-haspopup="dialog"
                        className="inline-flex min-h-[44px] items-center gap-1 rounded-lg px-3 text-sm font-semibold text-brand transition-colors hover:bg-white/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        See what&apos;s new
                        <ChevronRight size={16} aria-hidden="true" />
                    </button>
                    <button
                        type="button"
                        onClick={() => {
                            // The bar is about to go: focus moves to the
                            // header's "What's new" button, the nearest control
                            // that stays, not to <body>.
                            document.querySelector<HTMLElement>('[data-whats-new-header-button]')?.focus();
                            dismissBar();
                        }}
                        aria-label="Hide"
                        title="Hide until something new arrives"
                        className="inline-flex h-11 w-11 items-center justify-center rounded-lg text-brand-hover transition-colors hover:bg-white/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                        <X size={16} aria-hidden="true" />
                    </button>
                </div>
            </div>
        </div>
    );
};

export default SpotlightBar;
