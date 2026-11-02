// The "What's new" button in the Daily Briefing's header, next to Refresh
// (Homefeed.tsx). Same audience as the sidebar button: shown when What's New is
// on for the person and the feed has loaded.
import React from 'react';
import { Sparkles } from 'lucide-react';
import { whatsNewButtonLabel } from './logic';
import { openWhatsNew } from './store';
import { useWhatsNew } from './WhatsNewProvider';

export const WhatsNewHeaderButton: React.FC = () => {
    const { showButton, unseen } = useWhatsNew();
    if (!showButton) return null;
    const count = unseen.length;
    const label = whatsNewButtonLabel(count);

    return (
        <button
            type="button"
            onClick={() => openWhatsNew({ from: 'header' })}
            data-whats-new-header-button=""
            data-tour="home-whats-new"
            aria-label={label}
            title={label}
            aria-haspopup="dialog"
            className="flex items-center gap-2 px-4 py-2 border border-gray-200 bg-white text-gray-900 text-sm font-medium rounded-lg hover:bg-gray-50 transition-colors shadow-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
        >
            <Sparkles size={15} className="text-brand" aria-hidden="true" />
            What&apos;s new
            {count > 0 && (
                <span
                    className="min-w-[18px] h-[18px] px-1.5 rounded-full bg-brand text-white text-[11px] font-bold leading-[18px] text-center"
                    aria-hidden="true"
                >
                    {count} new
                </span>
            )}
        </button>
    );
};

export default WhatsNewHeaderButton;
