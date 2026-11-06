// The "What's new" button in the live sidebar's footer stack (Layout.tsx),
// just above the account footer.
//
// NOT gated on the copilot: What's New is company-wide, so EOS-only
// people get it too (unlike the Notifications bell beside it). Layout renders
// it only when `showButton` is true — the feature is on for this person and
// the feed has loaded, so an empty drawer still opens on "No updates yet."
//
// The badge counts updates new since the person last looked, and the count
// goes into the accessible name ("What's new, 3 new"), as the bell does.
import React from 'react';
import { Sparkles } from 'lucide-react';
import { badgeLabel } from '../notifications/rules';
import { whatsNewButtonLabel } from './logic';
import { openWhatsNew, useWhatsNewDrawer } from './store';
import { useWhatsNew } from './WhatsNewProvider';

export const WhatsNewButton: React.FC<{
    /** The nav panel is open, so the button has room for its label. */
    expanded: boolean;
}> = ({ expanded }) => {
    const { unseen, buttonRef } = useWhatsNew();
    const drawer = useWhatsNewDrawer();
    const count = unseen.length;
    const badge = badgeLabel(count);
    const label = whatsNewButtonLabel(count);
    const open = drawer.open && drawer.mode === 'full';

    return (
        <button
            ref={buttonRef}
            type="button"
            onClick={() => openWhatsNew({ from: 'button' })}
            data-tour="whats-new-button"
            title={label}
            aria-label={label}
            aria-haspopup="dialog"
            aria-expanded={open}
            className={`w-full flex items-center transition-colors hover:bg-white/5 focus:outline-none focus-visible:bg-white/10 ${
                open ? 'bg-white/10 text-white' : 'text-gray-200 hover:text-white'
            } ${expanded ? 'gap-2.5 px-4 py-2.5' : 'justify-center py-2.5'}`}
        >
            <span className="relative flex-shrink-0">
                <Sparkles size={expanded ? 17 : 19} aria-hidden="true" />
                {!expanded && badge && (
                    <span
                        className="absolute -top-1.5 -right-2.5 min-w-[18px] h-[18px] px-1 rounded-full bg-brand-soft text-brand-hover text-[10px] font-bold leading-[18px] text-center ring-2 ring-rail"
                        aria-hidden="true"
                    >
                        {badge}
                    </span>
                )}
            </span>
            {expanded && (
                <>
                    <span className="text-sm truncate">What&apos;s new</span>
                    <span className="ml-auto flex items-center gap-1.5">
                        {badge && (
                            <span
                                className="min-w-[20px] h-5 px-1.5 rounded-full bg-brand-soft text-brand-hover text-[11px] font-bold leading-5 text-center"
                                aria-hidden="true"
                            >
                                {badge}
                            </span>
                        )}
                    </span>
                </>
            )}
        </button>
    );
};

export default WhatsNewButton;
