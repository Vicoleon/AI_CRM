// "Built from: Gmail · 6 threads  HubSpot · 2 open deals …" — which sources
// actually informed this version of the brief. Omitted when there are none.
import React from 'react';
import { Layers } from 'lucide-react';

import { signalChipText, visibleSignals, type SignalUsed } from '../../pages/meetingPrep/snapshot';

export const BuiltFromStrip: React.FC<{ signals?: SignalUsed[] | null }> = ({ signals }) => {
    const shown = visibleSignals(signals);
    if (!shown.length) return null;
    return (
        <div className="flex flex-wrap items-center gap-1.5" aria-label="Sources this briefing was built from">
            <span className="mr-0.5 flex items-center gap-1 text-[10px] font-bold uppercase tracking-[0.15em] text-slate-400">
                <Layers className="h-3 w-3" />
                Built from
            </span>
            {shown.map(s => (
                <span
                    key={s.key || s.label}
                    className="rounded-full border border-slate-200 bg-white px-2.5 py-0.5 text-[11px] font-semibold text-slate-600"
                >
                    {signalChipText(s)}
                </span>
            ))}
        </div>
    );
};
