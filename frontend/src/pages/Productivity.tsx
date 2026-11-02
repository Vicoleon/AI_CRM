// UC-39 — Productivity route: full Email + Calendar + Slack + Documents
// tabs. Replaces the bottom 3-column row that used to live on the Homefeed.
import React, { useEffect, useRef, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { Mail, Calendar, FileBox, FolderTree, RefreshCw, AlertCircle, MessageSquare } from 'lucide-react';
import { useBriefing } from '../components/briefing/useBriefing';
import { useSlackActivity } from '../components/briefing/useSlackActivity';
import { warmProductivity } from '../components/briefing/productivityCache';
import { EmailMailboxPanel } from '../components/briefing/EmailMailboxPanel';
import { CalendarColumn } from '../components/briefing/CalendarColumn';
import { DocumentsColumn } from '../components/briefing/DocumentsColumn';
import { DepartmentFoldersColumn } from '../components/briefing/DepartmentFoldersColumn';
import { SlackColumn } from '../components/briefing/SlackColumn';
import { useAuth } from '../context/AuthContext';


type TabKey = 'email' | 'calendar' | 'slack' | 'documents' | 'team';


const TABS: { key: TabKey; label: string; icon: React.ReactNode }[] = [
    { key: 'email', label: 'Email', icon: <Mail size={16} /> },
    { key: 'calendar', label: 'Calendar', icon: <Calendar size={16} /> },
    { key: 'slack', label: 'Slack', icon: <MessageSquare size={16} /> },
    { key: 'documents', label: 'Documents', icon: <FileBox size={16} /> },
    { key: 'team', label: 'Team Folders', icon: <FolderTree size={16} /> },
    // UC-45's Customers tab moved out to its own Sales row (/customers) — this
    // page is your own inbox/calendar/documents, and account research was the
    // one thing here that wasn't.
];


function _tabFromParam(value: string | null): TabKey | null {
    return TABS.some(t => t.key === value) ? (value as TabKey) : null;
}


export const Productivity: React.FC = () => {
    const { user } = useAuth();
    const [searchParams, setSearchParams] = useSearchParams();
    // Honor a `?tab=` deep link (e.g. the Homefeed "next meeting" badge links to
    // ?tab=calendar). Falls back to Email when absent/unknown.
    const [active, setActive] = useState<TabKey>(
        () => _tabFromParam(searchParams.get('tab')) ?? 'email',
    );

    // Keep the active tab in the URL so it survives a drill-in + Back. Detail
    // pages (Meeting Prep, Customer X-Ray) are reached from a specific tab; when
    // the user comes back via history the URL must still carry the tab or the
    // page would remount on the default (Email). `replace` avoids stacking a
    // history entry per tab click — each click just re-stamps the current one.
    const selectTab = (key: TabKey) => {
        setActive(key);
        setSearchParams({ tab: key }, { replace: true });
    };
    // Email, Calendar, Slack and Documents read the cached copies Home warmed
    // after AI Priorities loaded, so a tab paints at once. A copy over a minute
    // old is refreshed quietly behind it (the Refresh button spins meanwhile).
    const { briefing, loading, refreshing, error, refresh } = useBriefing();
    const [emailRefreshNonce, setEmailRefreshNonce] = useState(0);
    // On a narrow window the tab bar scrolls sideways; keep the selected tab
    // in view so a deep link to ?tab=team never lands on a hidden tab.
    const activeTabRef = useRef<HTMLButtonElement | null>(null);
    useEffect(() => {
        activeTabRef.current?.scrollIntoView({ block: 'nearest', inline: 'center' });
    }, [active]);
    const {
        activity: slack,
        loading: slackLoading,
        refreshing: slackRefreshing,
        refresh: refreshSlack,
    } = useSlackActivity();
    // Landing here directly (a link, a reload) skips Home's warm-up; start the
    // tabs not on screen yet, Documents included, so switching is instant.
    useEffect(() => {
        warmProductivity();
    }, []);
    const busy = refreshing || slackRefreshing;

    const handleRefresh = () => {
        refresh();
        refreshSlack();
        setEmailRefreshNonce(value => value + 1);
    };

    // Keep the active tab in sync when the `?tab=` param changes while the page
    // is already mounted (repeated badge clicks, browser back/forward).
    useEffect(() => {
        const fromParam = _tabFromParam(searchParams.get('tab'));
        if (fromParam) setActive(fromParam);
    }, [searchParams]);

    // Customers left this page for a Sales row of its own. Bookmarks and older
    // links still carry `?tab=customers`; forward them instead of dropping the
    // reader on Email with no hint of where the screen went.
    if (searchParams.get('tab') === 'customers') {
        return <Navigate to="/customers" replace />;
    }

    return (
        <div className="h-full flex flex-col overflow-hidden bg-gray-50">
            {/* Header */}
            <div data-tour="productivity-header" className="px-8 pt-7 pb-5 flex items-center justify-between gap-4 border-b border-gray-100 bg-white">
                <div className="min-w-0">
                    <h1 className="text-2xl font-bold text-gray-900">Productivity</h1>
                    <p className="text-sm text-gray-500 mt-0.5">
                        Full views of your inbox, calendar, Slack and documents.
                    </p>
                </div>
                <button
                    data-tour="productivity-refresh"
                    onClick={handleRefresh}
                    disabled={busy}
                    className="flex-shrink-0 flex items-center gap-2 px-4 py-2 bg-brand text-white text-sm font-medium rounded-lg hover:bg-brand-hover disabled:opacity-50 transition-colors shadow-sm whitespace-nowrap"
                >
                    <RefreshCw size={15} className={busy ? 'animate-spin' : ''} />
                    Refresh
                </button>
            </div>

            {error && (
                <div className="mx-8 mt-4 p-3 bg-red-50 border border-red-200 rounded-lg flex items-center gap-2 text-sm text-red-700">
                    <AlertCircle size={16} />
                    {error}
                </div>
            )}

            {/* Tab bar. Scrolls sideways on a narrow window instead of letting
                the last tab (Team Folders) falls off the right edge. */}
            <div data-tour="productivity-tabs" className="px-8 mt-4 flex items-center gap-1 border-b border-gray-100 bg-white overflow-x-auto scrollbar-hide">
                {TABS.map(tab => {
                    const isActive = active === tab.key;
                    return (
                        <button
                            key={tab.key}
                            ref={isActive ? activeTabRef : undefined}
                            data-tour={tab.label === 'Slack' ? 'productivity-tab-slack' : tab.label === 'Documents' ? 'productivity-tab-documents' : undefined}
                            onClick={() => selectTab(tab.key)}
                            className={`flex-shrink-0 whitespace-nowrap px-4 py-3 flex items-center gap-2 text-sm font-medium transition-colors border-b-2 ${
                                isActive
                                    ? 'border-brand text-brand'
                                    : 'border-transparent text-gray-500 hover:text-gray-700'
                            }`}
                        >
                            {tab.icon}
                            {tab.label}
                        </button>
                    );
                })}
            </div>

            {/* Tab content */}
            <div className="flex-1 overflow-auto px-8 py-6">
                <div data-tour="productivity-email-inbox" className="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
                    {active === 'email' && (
                        <EmailMailboxPanel
                            key={`${user?.workspace?.slug ?? ''}:${user?.email ?? ''}`}
                            workEmails={briefing?.emails}
                            workLoading={loading}
                            workEmail={user?.email}
                            refreshNonce={emailRefreshNonce}
                        />
                    )}
                    {active === 'calendar' && (
                        <CalendarColumn
                            events={briefing?.events}
                            free_slots={briefing?.free_slots}
                            loading={loading}
                        />
                    )}
                    {active === 'slack' && (
                        <SlackColumn activity={slack} loading={slackLoading} />
                    )}
                    {active === 'documents' && <DocumentsColumn />}
                    {active === 'team' && <DepartmentFoldersColumn />}
                </div>
            </div>
        </div>
    );
};

export default Productivity;
