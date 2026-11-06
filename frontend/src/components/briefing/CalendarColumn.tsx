// Calendar column extracted from pages/Homefeed.tsx in UC-39 so it can
// be rendered as a tab in the new /productivity route.
//
// Offers two interchangeable views of the same events (toggle in the header):
//   - "List" — the original chronological agenda (default)
//   - "Week" — a 7-day week grid; events sit under their day column
// Both views preserve the "go to calendar" link and the "Get Ready"
// meeting-prep button.
import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Calendar, ExternalLink, BookOpen, List, CalendarDays, ChevronLeft, ChevronRight,
    Sparkles, Loader2, Check, AlertCircle,
} from 'lucide-react';
import type { CalendarEvent, FreeSlot } from './types';
import client from '../../api/client';
import { useAuth } from '../../context/AuthContext';
import { deptSlug } from '../../pages/eos/_utils';


// ─── Deep Prep button ───────────────────────────────────────────────────────
// Kicks the async, council-backed deep prep (emailed PDF) and polls its status.
// `viewParam` ("sales" | "regular") is only sent for the admin dev-preview
// toggle; the backend ignores it for non-admins.

type DeepState = 'idle' | 'working' | 'done' | 'error';

const DeepPrepButton: React.FC<{
    eventId: string;
    showSales: boolean;
    viewParam?: 'sales' | 'regular';
    compact?: boolean;
}> = ({ eventId, showSales, viewParam, compact }) => {
    const [state, setState] = useState<DeepState>('idle');
    const [msg, setMsg] = useState('');

    const run = async (e: React.MouseEvent) => {
        e.stopPropagation();
        e.preventDefault();
        if (state === 'working') return;
        setState('working');
        setMsg('');
        try {
            const q = viewParam ? `?view=${viewParam}` : '';
            const { data } = await client.post(
                `/api/homefeed/deep-prep/${encodeURIComponent(eventId)}${q}`,
            );
            const runId: string | undefined = data?.run_id;
            if (!runId) throw new Error('no run id');
            // Poll up to ~3 minutes (45 × 4s) for the background pipeline.
            for (let i = 0; i < 45; i++) {
                await new Promise(r => setTimeout(r, 4000));
                const res = await client.get(`/api/homefeed/deep-prep/${runId}/status`);
                const s: string = res.data?.status;
                if (s === 'delivered') { setState('done'); setMsg('Emailed'); return; }
                if (s === 'generated') { setState('done'); setMsg('Ready'); return; }
                if (s === 'failed') { setState('error'); setMsg('Failed'); return; }
            }
            setState('done'); setMsg('Sent');
        } catch (err: any) {
            setState('error');
            setMsg(err?.response?.status === 404 ? 'Off' : 'Retry');
        }
    };

    const base = showSales ? 'Sales deep prep' : 'Deep prep';
    const label =
        state === 'working' ? 'Preparing…'
        : state === 'done' ? (msg || 'Emailed')
        : state === 'error' ? (msg || 'Retry')
        : base;
    const Icon =
        state === 'working' ? Loader2
        : state === 'done' ? Check
        : state === 'error' ? AlertCircle
        : Sparkles;

    const size = compact ? 12 : 14;
    const cls = compact
        ? 'w-full flex items-center justify-center gap-1 text-[11px] font-semibold py-1.5 transition-all'
        : 'flex-shrink-0 flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-md transition-all';
    const tone =
        state === 'error' ? 'bg-amber-50 text-amber-700 hover:bg-amber-100'
        : state === 'done' ? 'bg-emerald-600 text-white'
        : 'bg-brand text-white hover:bg-brand-hover';

    return (
        <button
            onClick={run}
            disabled={state === 'working'}
            className={`${cls} ${tone}`}
            title={showSales ? 'Generate a deep sales prep, emailed as a PDF'
                             : 'Generate a deep meeting prep, emailed as a PDF'}
        >
            <Icon size={size} className={state === 'working' ? 'animate-spin' : ''} />
            {label}
        </button>
    );
};


function formatTime(iso: string): string {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
        return iso;
    }
}

function formatEventDate(iso: string): string {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    } catch {
        return iso;
    }
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// Stable local-day key, e.g. "2026-5-24" (month is 0-based — grouping only).
function dayKey(d: Date): string {
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

// Sunday that starts the week containing `d` (local time).
function startOfWeek(d: Date): Date {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate() - d.getDay());
}


// ─── RSVP state ────────────────────────────────────────────────────────────────
// Your own response to the invite. A meeting you DECLINED gets no prep buttons —
// you already made that call. An unanswered one keeps them (you may well be
// prepping in order to decide) but is badged, because it's the state that makes
// a calendar noisy. The AI Priorities feed is stricter: it never volunteers prep
// for an unanswered invite, since that's Cubie pushing rather than you asking.

const canPrep = (ev: CalendarEvent): boolean => ev.rsvp !== 'declined';

const RsvpBadge: React.FC<{ ev: CalendarEvent; compact?: boolean }> = ({ ev, compact }) => {
    if (ev.rsvp !== 'declined' && ev.rsvp !== 'needs_action') return null;
    const declined = ev.rsvp === 'declined';
    return (
        <span
            className={`inline-flex items-center rounded-full font-medium ${
                compact ? 'text-[9px] px-1.5' : 'text-[10px] px-2 py-0.5'
            } ${declined
                ? 'bg-gray-100 text-gray-500'
                : 'bg-amber-100 text-amber-700'
            }`}
            title={declined
                ? "You declined this invite — Cubie won't prep it"
                : "You haven't responded to this invite yet. Accept or decline to keep your daily prep clean."}
        >
            {declined ? 'Declined' : 'No reply'}
        </span>
    );
};


// ─── List-view event row ───────────────────────────────────────────────────────

const EventRow: React.FC<{
    ev: CalendarEvent;
    navigate: ReturnType<typeof useNavigate>;
    showSales: boolean;
    viewParam?: 'sales' | 'regular';
}> = ({ ev, navigate, showSales, viewParam }) => {
    const evDate = ev.start ? new Date(ev.start) : null;
    const calUrl = evDate
        ? `https://calendar.google.com/calendar/r/day/${evDate.getFullYear()}/${evDate.getMonth() + 1}/${evDate.getDate()}`
        : 'https://calendar.google.com/calendar/r';
    return (
        <div className="px-5 py-3 hover:bg-emerald-50/30 transition-colors group flex items-start gap-2">
            <a
                href={calUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex-1 min-w-0"
            >
                <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                        <div className="flex items-center gap-1.5 min-w-0">
                            <p className="text-sm font-medium text-gray-800 group-hover:text-emerald-700 transition-colors truncate">{ev.title}</p>
                            <RsvpBadge ev={ev} />
                        </div>
                        <p className="text-xs text-gray-500 mt-0.5">
                            {ev.all_day
                                ? `${formatEventDate(ev.start)} · All day`
                                : `${formatEventDate(ev.start)} · ${formatTime(ev.start)} – ${formatTime(ev.end)}`
                            }
                        </p>
                        {ev.location && (
                            <p className="text-xs text-gray-400 mt-0.5 truncate">{ev.location}</p>
                        )}
                    </div>
                    <ExternalLink size={11} className="text-gray-300 group-hover:text-emerald-400 transition-colors flex-shrink-0 mt-0.5" />
                </div>
            </a>
            {!ev.all_day && canPrep(ev) && (
                <div className="flex flex-col gap-1.5 flex-shrink-0">
                    <button
                        onClick={() => navigate(`/meeting-prep/${encodeURIComponent(ev.event_id || '')}`, { state: { event: ev } })}
                        className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-md bg-brand/10 text-brand hover:bg-brand hover:text-white transition-all"
                        title="Get ready for this meeting"
                    >
                        <BookOpen size={14} />
                        Get Ready
                    </button>
                    <DeepPrepButton
                        eventId={ev.event_id || ''}
                        showSales={showSales}
                        viewParam={viewParam}
                    />
                </div>
            )}
        </div>
    );
};


// ─── Free slots (shown beneath both views) ─────────────────────────────────────

const FreeSlots: React.FC<{ free_slots?: FreeSlot[] }> = ({ free_slots }) => {
    if ((free_slots || []).length === 0) return null;
    return (
        <div className="px-5 py-4 border-t border-gray-100">
            <p className="text-xs font-semibold text-emerald-700 uppercase tracking-wider mb-3">
                Free Slots (9 AM – 6 PM)
            </p>
            <div className="space-y-3">
                {free_slots!.map((day, i) => (
                    <div key={i}>
                        <p className="text-xs font-semibold text-gray-600 mb-1">{day.date}</p>
                        <div className="flex flex-wrap gap-1.5">
                            {day.slots.map((slot, j) => (
                                <span
                                    key={j}
                                    className="text-xs bg-emerald-100 text-emerald-800 px-2 py-0.5 rounded-full font-medium"
                                >
                                    {slot}
                                </span>
                            ))}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
};


// ─── Week-view event card (one per event, inside its day column) ───────────────

const WeekEventCard: React.FC<{
    ev: CalendarEvent;
    navigate: ReturnType<typeof useNavigate>;
    showSales: boolean;
    viewParam?: 'sales' | 'regular';
}> = ({ ev, navigate, showSales, viewParam }) => {
    const evDate = ev.start ? new Date(ev.start) : null;
    const calUrl = evDate
        ? `https://calendar.google.com/calendar/r/day/${evDate.getFullYear()}/${evDate.getMonth() + 1}/${evDate.getDate()}`
        : 'https://calendar.google.com/calendar/r';
    return (
        <div className="group rounded-md border border-emerald-100 bg-emerald-50 hover:bg-emerald-100 transition-colors overflow-hidden">
            <a
                href={calUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="block px-2 py-1.5"
                title={ev.location ? `${ev.title} · ${ev.location}` : ev.title}
            >
                <div className="flex items-center justify-between gap-1">
                    <p className="text-[10px] font-semibold text-emerald-700/90">
                        {ev.all_day ? 'All day' : formatTime(ev.start)}
                    </p>
                    <RsvpBadge ev={ev} compact />
                </div>
                <p className="text-[11px] font-medium text-emerald-900 leading-snug line-clamp-2">
                    {ev.title}
                </p>
            </a>
            {!ev.all_day && canPrep(ev) && (
                <>
                    <button
                        onClick={() => navigate(`/meeting-prep/${encodeURIComponent(ev.event_id || '')}`, { state: { event: ev } })}
                        className="w-full flex items-center justify-center gap-1 text-[11px] font-semibold py-1.5 bg-brand/10 text-brand hover:bg-brand hover:text-white transition-all"
                        title="Get ready for this meeting"
                    >
                        <BookOpen size={12} />
                        Get Ready
                    </button>
                    <DeepPrepButton
                        eventId={ev.event_id || ''}
                        showSales={showSales}
                        viewParam={viewParam}
                        compact
                    />
                </>
            )}
        </div>
    );
};


// ─── Week-grid calendar view ───────────────────────────────────────────────────

const WeekCalendar: React.FC<{
    events: CalendarEvent[];
    navigate: ReturnType<typeof useNavigate>;
    showSales: boolean;
    viewParam?: 'sales' | 'regular';
}> = ({ events, navigate, showSales, viewParam }) => {
    const today = new Date();

    // Group events by local calendar day, each day sorted by start time.
    const eventsByDay = useMemo(() => {
        const map: Record<string, CalendarEvent[]> = {};
        for (const ev of events) {
            if (!ev.start) continue;
            const d = new Date(ev.start);
            if (isNaN(d.getTime())) continue;
            const k = dayKey(d);
            (map[k] ||= []).push(ev);
        }
        for (const k of Object.keys(map)) {
            map[k].sort((a, b) => new Date(a.start).getTime() - new Date(b.start).getTime());
        }
        return map;
    }, [events]);

    // Open on the week of the first upcoming event, falling back to this week.
    const initial = useMemo(() => {
        const first = events.find(e => e.start && !isNaN(new Date(e.start).getTime()));
        return first ? new Date(first.start) : new Date();
    }, [events]);

    const [weekStart, setWeekStart] = useState<Date>(() => startOfWeek(initial));

    const shiftWeek = (delta: number) =>
        setWeekStart(prev => new Date(prev.getFullYear(), prev.getMonth(), prev.getDate() + delta * 7));
    const goThisWeek = () => setWeekStart(startOfWeek(new Date()));

    const days = Array.from({ length: 7 }, (_, i) =>
        new Date(weekStart.getFullYear(), weekStart.getMonth(), weekStart.getDate() + i));
    const weekEnd = days[6];
    const rangeLabel = `${weekStart.toLocaleDateString([], { month: 'short', day: 'numeric' })} – ${weekEnd.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}`;
    const todayKey = dayKey(today);

    return (
        <div className="p-4 md:p-6">
            {/* Week navigation */}
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-1">
                    <button
                        onClick={() => shiftWeek(-1)}
                        className="p-1.5 rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700 transition-colors"
                        title="Previous week"
                    >
                        <ChevronLeft size={18} />
                    </button>
                    <button
                        onClick={() => shiftWeek(1)}
                        className="p-1.5 rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700 transition-colors"
                        title="Next week"
                    >
                        <ChevronRight size={18} />
                    </button>
                    <h3 className="text-base font-semibold text-gray-800 ml-2">{rangeLabel}</h3>
                </div>
                <button
                    onClick={goThisWeek}
                    className="text-xs font-medium px-3 py-1.5 rounded-md border border-gray-200 text-gray-600 hover:bg-gray-50 transition-colors"
                >
                    This week
                </button>
            </div>

            {/* 7-day grid */}
            <div className="overflow-x-auto">
                <div className="grid grid-cols-7 gap-2 min-w-[720px]">
                    {days.map(day => {
                        const k = dayKey(day);
                        const evs = eventsByDay[k] || [];
                        const isToday = k === todayKey;
                        return (
                            <div key={k} className="flex flex-col">
                                {/* Column header */}
                                <div className={`text-center pb-2 mb-2 border-b-2 ${isToday ? 'border-emerald-500' : 'border-gray-100'}`}>
                                    <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                                        {WEEKDAYS[day.getDay()]}
                                    </p>
                                    <span className={`mt-0.5 inline-flex items-center justify-center text-sm w-7 h-7 rounded-full ${
                                        isToday ? 'bg-emerald-600 text-white font-semibold' : 'text-gray-700'
                                    }`}>
                                        {day.getDate()}
                                    </span>
                                </div>
                                {/* Events for the day */}
                                <div className="space-y-1.5 min-h-[60px]">
                                    {evs.length === 0 ? (
                                        <p className="text-center text-[11px] text-gray-300 pt-2">—</p>
                                    ) : (
                                        evs.map((ev, j) => (
                                            <WeekEventCard
                                                key={ev.event_id || j}
                                                ev={ev}
                                                navigate={navigate}
                                                showSales={showSales}
                                                viewParam={viewParam}
                                            />
                                        ))
                                    )}
                                </div>
                            </div>
                        );
                    })}
                </div>
            </div>
        </div>
    );
};


export const CalendarColumn: React.FC<{
    events?: CalendarEvent[];
    free_slots?: FreeSlot[];
    loading?: boolean;
}> = ({ events, free_slots, loading }) => {
    const navigate = useNavigate();
    const { user, isAdmin } = useAuth();
    const [view, setView] = useState<'list' | 'week'>('week');
    // Dev/admin only: preview the regular vs. sales prep button without changing
    // your account. 'self' = your real role.
    const [viewAs, setViewAs] = useState<'self' | 'regular' | 'sales'>('self');

    // Picks the prep button's flavour, not who may use it: compared by
    // department slug ("Sales" ≡ " sales "), like every department check.
    const ownIsSales =
        deptSlug(user?.department) === 'sales'
        || (user?.roles?.some(r => deptSlug(r) === 'sales') ?? false);
    const previewing = isAdmin && viewAs !== 'self';
    const showSales = previewing ? viewAs === 'sales' : ownIsSales;
    const viewParam: 'sales' | 'regular' | undefined = previewing
        ? (viewAs === 'sales' ? 'sales' : 'regular')
        : undefined;

    if (loading) {
        return (
            <div className="animate-pulse space-y-3 p-4">
                {Array.from({ length: 4 }).map((_, i) => (
                    <div key={i} className={`h-3 bg-gray-200 rounded ${i === 0 ? 'w-3/4' : i % 2 === 0 ? 'w-full' : 'w-5/6'}`} />
                ))}
            </div>
        );
    }

    return (
        <div>
            {/* Header: dev/admin "view as" toggle (left) + List/Week toggle (right) */}
            <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-gray-100">
                {isAdmin ? (
                    <div className="flex items-center gap-1" title="Dev/admin: preview the prep button non-sales vs. sales users see">
                        <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-400 mr-1">View as</span>
                        {(['self', 'regular', 'sales'] as const).map(v => (
                            <button
                                key={v}
                                onClick={() => setViewAs(v)}
                                className={`text-[11px] font-medium px-2 py-1 rounded-md transition-colors ${
                                    viewAs === v ? 'bg-brand text-white' : 'text-gray-500 hover:bg-gray-100'
                                }`}
                            >
                                {v === 'self' ? 'Mine' : v === 'regular' ? 'Regular' : 'Sales'}
                            </button>
                        ))}
                    </div>
                ) : <span />}
                <div className="flex items-center gap-1">
                    <button
                        onClick={() => setView('list')}
                        className={`flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-md transition-colors ${
                            view === 'list' ? 'bg-emerald-100 text-emerald-800' : 'text-gray-500 hover:bg-gray-100'
                        }`}
                    >
                        <List size={14} /> List
                    </button>
                    <button
                        onClick={() => setView('week')}
                        className={`flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-md transition-colors ${
                            view === 'week' ? 'bg-emerald-100 text-emerald-800' : 'text-gray-500 hover:bg-gray-100'
                        }`}
                    >
                        <CalendarDays size={14} /> Week
                    </button>
                </div>
            </div>

            {view === 'list' ? (
                <div className="divide-y divide-gray-50">
                    {(events || []).slice(0, 10).map((ev, i) => (
                        <EventRow
                            key={ev.event_id || i}
                            ev={ev}
                            navigate={navigate}
                            showSales={showSales}
                            viewParam={viewParam}
                        />
                    ))}
                </div>
            ) : (
                <WeekCalendar
                    events={events || []}
                    navigate={navigate}
                    showSales={showSales}
                    viewParam={viewParam}
                />
            )}

            {/* Availability shown in both views */}
            <FreeSlots free_slots={free_slots} />
        </div>
    );
};

export default CalendarColumn;
export { Calendar };
