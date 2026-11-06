import React, { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Mail, Calendar, Clock, ChevronRight, MessageSquare, AtSign } from 'lucide-react';
import type { SlackActivity } from './briefing/useSlackActivity';

// ─── Tunable thresholds (UC-39) ──────────────────────────────────────────────
//
// Named constants so this is easy to revisit when user feedback rolls in.
// Above the threshold the badge flips to amber as a soft "heads up" — never
// blocks, never bothers; the strip always renders the real count.
export const UNREAD_THRESHOLD = 5;
export const NEXT_MEETING_HOURS = 3;
// Slack "you're being addressed" badge flips to amber once you've piled up a
// few unanswered mentions/DMs.
export const SLACK_ATTENTION_THRESHOLD = 3;


export interface BriefingSummary {
    unread_count: number;
    next_event?: {
        title: string;
        start_iso?: string;
    } | null;
    free_slot_count_today?: number;
}


function _formatNextMeetingDelta(startIso?: string): { label: string; hours: number | null } {
    if (!startIso) return { label: 'no upcoming meeting', hours: null };
    try {
        const start = new Date(startIso);
        const minutes = (start.getTime() - Date.now()) / 60_000;
        if (Number.isNaN(minutes)) return { label: 'no upcoming meeting', hours: null };
        if (minutes < 0) return { label: 'in progress', hours: 0 };
        if (minutes < 60) return { label: `next meeting in ${Math.round(minutes)} min`, hours: minutes / 60 };
        const hours = minutes / 60;
        const rounded = Math.round(hours * 10) / 10;
        return {
            label: `next meeting in ${rounded < 1 ? rounded.toFixed(1) : Math.round(rounded)} hour${Math.round(rounded) === 1 ? '' : 's'}`,
            hours,
        };
    } catch {
        return { label: 'no upcoming meeting', hours: null };
    }
}


export const TodayStrip: React.FC<{
    briefing: BriefingSummary | null;
    slack?: SlackActivity | null;
}> = ({ briefing, slack }) => {
    const navigate = useNavigate();

    // "Things directed at you" = @-mentions + unread DMs. Matches the homepage
    // ask ("you have been mentioned in N places"); the Slack tab breaks the two
    // apart. Channels-with-unread is its own, lighter signal.
    const slackConnected = !!slack?.is_connected && !slack?.error;
    const slackAttention = slackConnected ? slack!.mention_count + slack!.dm_count : 0;
    const slackChannels = slackConnected ? slack!.channel_count : 0;
    const slackAttentionClass =
        slackAttention >= SLACK_ATTENTION_THRESHOLD
            ? 'bg-amber-100 text-amber-700'
            : 'bg-brand-soft/60 text-brand-hover';

    const unreadBadgeClass = useMemo(() => {
        if (!briefing) return 'bg-gray-100 text-gray-600';
        return briefing.unread_count > UNREAD_THRESHOLD
            ? 'bg-amber-100 text-amber-700'
            : 'bg-gray-100 text-gray-700';
    }, [briefing]);

    const nextMeeting = useMemo(
        () => _formatNextMeetingDelta(briefing?.next_event?.start_iso),
        [briefing?.next_event?.start_iso],
    );

    const meetingBadgeClass = useMemo(() => {
        if (nextMeeting.hours === null) return 'bg-gray-100 text-gray-600';
        return nextMeeting.hours < NEXT_MEETING_HOURS
            ? 'bg-amber-100 text-amber-700'
            : 'bg-gray-100 text-gray-700';
    }, [nextMeeting.hours]);

    const freeSlotsLabel = briefing?.free_slot_count_today != null
        ? `${briefing.free_slot_count_today} free ${briefing.free_slot_count_today === 1 ? 'slot' : 'slots'} today`
        : '';

    // Each badge deep-links to the matching Productivity tab — the meeting and
    // free-slot badges go to Calendar, the unread badge to Email. (Previously
    // the whole strip was one button to /productivity, which always landed on
    // the default Email tab — so clicking "next meeting" opened the inbox.)
    const badgeBase =
        'text-[11px] font-semibold uppercase tracking-tight px-2 py-1 rounded-full inline-flex items-center gap-1 transition-shadow hover:ring-2 hover:ring-amber-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-300';

    return (
        <div data-tour="home-today-strip" className="w-full bg-white rounded-2xl border border-gray-100 shadow-sm px-5 py-3 flex items-center flex-wrap gap-3">
            <button
                type="button"
                onClick={() => navigate('/productivity?tab=email')}
                className={`${badgeBase} ${unreadBadgeClass}`}
                title="Open inbox"
            >
                <Mail size={12} />
                {briefing?.unread_count ?? 0} unread
            </button>

            <span className="text-gray-300">·</span>

            <button
                type="button"
                onClick={() => navigate('/productivity?tab=calendar')}
                className={`${badgeBase} ${meetingBadgeClass}`}
                title="Open calendar"
            >
                <Calendar size={12} />
                {nextMeeting.label}
            </button>

            {freeSlotsLabel && (
                <>
                    <span className="text-gray-300">·</span>
                    <button
                        type="button"
                        onClick={() => navigate('/productivity?tab=calendar')}
                        className={`${badgeBase} bg-gray-100 text-gray-700`}
                        title="Open calendar"
                    >
                        <Clock size={12} />
                        {freeSlotsLabel}
                    </button>
                </>
            )}

            {slackAttention > 0 && (
                <>
                    <span className="text-gray-300">·</span>
                    <button
                        type="button"
                        onClick={() => navigate('/productivity?tab=slack')}
                        className={`${badgeBase} ${slackAttentionClass}`}
                        title={`${slack!.mention_count} mention${slack!.mention_count === 1 ? '' : 's'} · ${slack!.dm_count} direct message${slack!.dm_count === 1 ? '' : 's'} on Slack`}
                    >
                        <AtSign size={12} />
                        {slackAttention} {slackAttention === 1 ? 'mention' : 'mentions'}
                    </button>
                </>
            )}

            {slackChannels > 0 && (
                <>
                    <span className="text-gray-300">·</span>
                    <button
                        type="button"
                        onClick={() => navigate('/productivity?tab=slack')}
                        className={`${badgeBase} bg-brand-soft/60 text-brand-hover`}
                        title="Channels with unread Slack messages"
                    >
                        <MessageSquare size={12} />
                        {slackChannels} Slack {slackChannels === 1 ? 'channel' : 'channels'}
                    </button>
                </>
            )}

            <button
                type="button"
                onClick={() => navigate('/productivity')}
                className="ml-auto text-xs text-gray-400 inline-flex items-center gap-0.5 hover:text-amber-600 transition-colors"
            >
                Open Productivity <ChevronRight size={14} />
            </button>
        </div>
    );
};

export default TodayStrip;
