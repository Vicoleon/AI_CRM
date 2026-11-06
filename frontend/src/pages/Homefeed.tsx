import React, { useCallback, useEffect, useMemo } from 'react';
import { useAuth } from '../context/AuthContext';
import { automationStatus } from '../auth/automation';
import { RefreshCw, AlertCircle } from 'lucide-react';
import { AIPrioritiesPanel } from '../components/AIPrioritiesPanel';
import { TodayStrip, type BriefingSummary } from '../components/TodayStrip';
import { useBriefing } from '../components/briefing/useBriefing';
import { useSlackActivity } from '../components/briefing/useSlackActivity';
import { warmProductivity } from '../components/briefing/productivityCache';
import { SpotlightBar } from '../components/whatsNew/SpotlightBar';
import { WhatsNewHeaderButton } from '../components/whatsNew/WhatsNewHeaderButton';


// ─── Helpers ─────────────────────────────────────────────────────────────────

function greeting(): string {
    const h = new Date().getHours();
    if (h < 12) return 'Good morning';
    if (h < 17) return 'Good afternoon';
    return 'Good evening';
}


// AI Priorities gets the first few seconds to itself; after that the
// Productivity warm-up starts even if priorities is still answering.
const PRIORITIES_HEADSTART_MS = 6_000;


// ─── Main Page ────────────────────────────────────────────────────────────────
//
// UC-39 layout:
//   1. Header (greeting + refresh)
//   2. AI Priorities panel (UC-20→25 multi-agent pipeline)
//   3. Today strip (compact glance at unread / next meeting / free slots,
//      links to /productivity for full views)
//
// What used to be the bottom 3-column grid (Inbox / Action Items / Calendar)
// has been removed:
//   - Action Items deleted — strictly weaker duplication of AI Priorities
//   - Inbox + Calendar moved to the /productivity route as full-page tabs
//     using the same data via the shared `useBriefing` hook + the extracted
//     `<InboxColumn>` / `<CalendarColumn>` components.

export const Homefeed: React.FC = () => {
    const { user } = useAuth();
    const automationsEnabled = automationStatus(user) === 'enabled';
    const warmAutomaticProductivity = useCallback(() => {
        if (automationsEnabled) warmProductivity();
    }, [automationsEnabled]);
    // The Today strip reads the same cached Email + Calendar and Slack copies
    // as Productivity, but leaves the fetching to warmProductivity: AI
    // Priorities loads first, then the warm-up gets Email, Calendar, Slack and
    // Documents ready in the background. The strip fills in from that, and
    // opening Productivity afterwards paints from it. Slack stays on its own
    // track so its per-conversation latency never blocks the briefing.
    const { briefing, loading, refreshing, error, refresh } = useBriefing({ load: false });
    const { activity: slack } = useSlackActivity({ load: false });
    useEffect(() => {
        if (!automationsEnabled) return;
        const t = window.setTimeout(warmAutomaticProductivity, PRIORITIES_HEADSTART_MS);
        return () => window.clearTimeout(t);
    }, [automationsEnabled, warmAutomaticProductivity]);

    const firstName = user?.name?.split(' ')[0] || 'there';
    const today = new Date().toLocaleDateString([], {
        weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
    });

    const summary = useMemo<BriefingSummary | null>(() => {
        if (!briefing) return null;
        const unread_count = briefing.emails.filter(e => e.is_unread).length;
        const next_event = briefing.events.find(ev => !ev.all_day && ev.start) || null;
        // Count "today" free slots only — the first FreeSlot row covers today
        // by the backend convention (4-day lookahead, ordered).
        const today_slots = briefing.free_slots[0]?.slots || [];
        const free_slot_count_today = today_slots.filter(
            s => s !== 'Fully booked or no data'
        ).length;
        return {
            unread_count,
            next_event: next_event
                ? { title: next_event.title, start_iso: next_event.start }
                : null,
            free_slot_count_today,
        };
    }, [briefing]);

    return (
        <div className="h-full flex flex-col overflow-hidden bg-gray-50">
            {/* Header */}
            <div data-tour="home-header" className="px-8 pt-7 pb-5 flex items-center justify-between border-b border-gray-100 bg-white">
                <div>
                    <h1 className="text-2xl font-bold text-gray-900">
                        {greeting()}, {firstName} 👋
                    </h1>
                    <p className="text-sm text-gray-500 mt-0.5">{today}</p>
                </div>
                <div className="flex items-center gap-2">
                    {/* Opens the What's New drawer; renders itself only when
                        there is something to read. */}
                    <WhatsNewHeaderButton />
                    <button
                        onClick={refresh}
                        disabled={loading || refreshing}
                        className="flex items-center gap-2 px-4 py-2 bg-brand text-white text-sm font-medium rounded-lg hover:bg-brand-hover disabled:opacity-50 transition-colors shadow-sm"
                    >
                        <RefreshCw size={15} className={loading || refreshing ? 'animate-spin' : ''} />
                        Refresh
                    </button>
                </div>
            </div>

            {/* What's New spotlight — one line naming the newest update this
                person hasn't seen. Renders itself only when there is one. */}
            <SpotlightBar />

            {/* Error banner */}
            {error && (
                <div className="mx-8 mt-4 p-3 bg-red-50 border border-red-200 rounded-lg flex items-center gap-2 text-sm text-red-700">
                    <AlertCircle size={16} />
                    {error}
                </div>
            )}

            {/* Scrollable body: AI Priorities + Today strip */}
            <div className="flex-1 overflow-y-auto">
                {/* AI Priorities — multi-agent pipeline panel */}
                <div className="px-8 mt-6">
                    <AIPrioritiesPanel onSettled={warmAutomaticProductivity} />
                </div>

                {/* Today strip — compact glance + tap to open /productivity */}
                <div className="px-8 mt-4 mb-6">
                    <TodayStrip briefing={summary} slack={slack} />
                </div>
            </div>
        </div>
    );
};
