// Meeting Prep — the briefing for one calendar meeting, with its versions.
//
// Opening the page shows the reader's LATEST version of this meeting's brief;
// Cubie only builds one (v1) the first time. Every change after that is a new
// version and nothing is overwritten:
//
//   * Regenerate — a full rebuild from today's signals. Sections the reader
//     edited by hand are carried over unless they untick "Keep my edits".
//   * Edit / Rewrite on one section — in place, on the same canvas.
//   * Restore — an older version becomes the latest again (as a new copy).
//
// Full builds (v1, and every Regenerate) run in the BACKGROUND: the server
// answers at once with the build's handle, the page polls its status every
// few seconds, and the reader is free to leave — Cubie sends an in-app
// notification with a link when the version is ready (or could not be built).
// While v1 builds the page says so and offers the way out; while a Regenerate
// builds, the current version stays on screen under a banner, and the new one
// replaces it when it lands (unless an unsaved edit is open — then the banner
// offers it instead). A build already in flight when the page opens (another
// tab, or before the reader left) is picked up again.
//
// Older versions open read-only, with a banner to restore them or go back.
// Word, Save to Assets and PDF act on the version on screen.
//
// Loading follows the derived-state pattern (see SmartTeamsSupport.tsx): the
// first-load spinner is "an event id with no settled result for it yet", the
// loaded brief (and the build in flight) are stamped with the event they
// belong to, and a ref names the event whose responses may still write state —
// so the effect keyed on the route param never sets state synchronously.
import React, { useCallback, useEffect, useEffectEvent, useRef, useState } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import {
    AlertCircle,
    ArrowRight,
    BookOpen,
    Briefcase,
    Calendar,
    ClipboardList,
    Clock,
    ExternalLink,
    FileDown,
    FileText,
    FolderPlus,
    History,
    Loader2,
    MailWarning,
    MapPin,
    MessageSquare,
    Mic,
    Plus,
    Radar,
    RefreshCw,
    RotateCcw,
    Search,
    ShieldAlert,
    Sparkles,
    Target,
    UserPlus,
    Users,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import api from '../api/client';
import {
    docxFilename,
    downloadDocx,
    editSection,
    getBuildStatus,
    getPrep,
    getVersion,
    isStaleVersion,
    listVersions,
    newerVersion,
    readPrepError,
    regenerate,
    restoreVersion,
    rewriteSection,
    saveToAssets as saveToAssetsApi,
    STILL_WORKING_MESSAGE,
    UNSAVED_MESSAGE,
    writeOutcome,
    type BuildStatus,
    type MeetingPrepData,
    type PendingBuild,
    type SectionValue,
    type VersionList,
    type VersionSummary,
} from '../api/meetingPrep';
import { FeedbackButtons, type FeedbackRating } from '../components/FeedbackButtons';
import { BackButton } from '../components/BackButton';
import {
    AttendeeIntelPanel,
    LinkedInLink,
    hasIntelContent,
    indexIntelByEmail,
    type AttendeeIntel,
} from '../components/AttendeeIntel';
import { toast } from '../components/eos/Toast';
import { AccountSnapshotCard } from '../components/meetingPrep/AccountSnapshotCard';
import { BuildBanner, BuildingPanel } from '../components/meetingPrep/BuildProgress';
import { BuiltFromStrip } from '../components/meetingPrep/BuiltFromStrip';
import { EditableSection } from '../components/meetingPrep/EditableSection';
import { SectionCard } from '../components/meetingPrep/SectionCard';
import { VersionMenu } from '../components/meetingPrep/VersionMenu';
import { cn } from '../components/meetingPrep/cn';
import {
    MEETING_PREP_FAILED,
    isMeetingPrepKind,
    meetingPrepEventId,
    prepNoticeStep,
    unackedPrepNotices,
} from '../components/notifications/rules';
import { useNotifications } from '../context/NotificationsContext';
import { downloadBlob } from '../utils/download';
import {
    BUILD_LOST,
    FIRST_BUILD_FAILED,
    REGENERATE_FAILED,
    isSettled,
    nextPollDelay,
    readyAction,
    staleBuilds,
} from './meetingPrep/build';
import {
    describeChange,
    editedSectionKeys,
    formatVersionWhen,
    isGeneration,
    normalizeProseContent,
    parseStakeholderMap,
    sectionLabel,
    type SectionKey,
    type Stakeholder,
} from './meetingPrep/sections';

// ─── Read views (unchanged look) ──────────────────────────────────────────

/**
 * Clean a stakeholder name coming from the model:
 * - strips markdown bold markers (**)
 * - if the result looks like an email, Title Cases the local part
 */
function cleanStakeholderName(raw: string): string {
    if (!raw) return raw;
    const stripped = raw.replace(/\*\*/g, '').trim();
    if (stripped.includes('@')) {
        const local = stripped.split('@')[0];
        return local
            .replace(/[._-]+/g, ' ')
            .split(' ')
            .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
            .join(' ');
    }
    return stripped;
}

/** Render any string field through ReactMarkdown with consistent prose styling */
const ProseField: React.FC<{ content: string; dark?: boolean; size?: 'sm' | 'base' }> = ({
    content,
    dark,
    size = 'base',
}) => (
    <div
        className={cn(
            'prose max-w-none leading-relaxed',
            size === 'sm' ? 'prose-sm' : 'prose-base',
            dark ? 'prose-invert' : 'prose-slate',
            '[&_ul]:space-y-1.5 [&_li]:text-current [&_li_p]:my-0 [&_p]:my-1',
        )}
    >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{normalizeProseContent(content)}</ReactMarkdown>
    </div>
);

/** likely_objections as individual numbered cards */
const ObjectionsList: React.FC<{ content: string }> = ({ content }) => {
    const normalized = normalizeProseContent(content);
    const items = normalized
        .split(/\n(?=[-*•]|\d+\.)/)
        .map(s => s.replace(/^[-*•]\s*|\d+\.\s*/, '').trim())
        .filter(Boolean);

    if (items.length <= 1) return <ProseField content={content} size="sm" />;

    return (
        <ul className="space-y-2">
            {items.map((objection, idx) => (
                <li key={idx} className="flex gap-3 items-start group">
                    <div className="mt-1 shrink-0 w-5 h-5 rounded-full bg-brand/10 flex items-center justify-center group-hover:bg-brand/20 transition-colors">
                        <span className="text-brand text-[10px] font-black">{idx + 1}</span>
                    </div>
                    <div className="text-sm text-slate-600 leading-relaxed prose prose-sm max-w-none [&_p]:my-0">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{objection}</ReactMarkdown>
                    </div>
                </li>
            ))}
        </ul>
    );
};

/**
 * One person in the stakeholder map.
 *
 * `intel` is the UC-47 research for this same email, when there is any. It only
 * ever ADDS to the card: the researched title fills in for a role the model left
 * blank (never overrides one it produced, which is grounded in the meeting
 * context the research doesn't see), and the headline/LinkedIn link give the
 * reader somewhere to click. Deep detail stays in the Attendee Intelligence
 * panel — this card's job is still the meeting dynamic.
 */
const StakeholderCard: React.FC<{ person: Stakeholder; intel?: AttendeeIntel }> = ({ person, intel }) => {
    const displayName = cleanStakeholderName(person.name || '');
    const researched = hasIntelContent(intel) ? intel : undefined;
    const profile = researched?.profile;
    const role = person.role || [profile?.current_title, profile?.current_company].filter(Boolean).join(' @ ');

    return (
        <div className="p-6 bg-slate-50/50 rounded-[1.5rem] border border-slate-200/60 flex flex-col gap-4 hover:border-brand/40 transition-all group/person hover:shadow-xl hover:shadow-slate-200/30">
            <div className="flex items-start gap-4">
                <div className="w-14 h-14 shrink-0 rounded-2xl bg-gradient-to-br from-brand to-brand-hover text-white flex items-center justify-center font-black shadow-lg shadow-brand/20 group-hover/person:scale-110 transition-transform">
                    {displayName ? displayName.slice(0, 2).toUpperCase() : '??'}
                </div>
                <div className="min-w-0 flex-1 pt-1">
                    <h4 className="text-lg font-black text-slate-900 truncate leading-none mb-1">{displayName}</h4>
                    {role && <p className="text-xs text-brand font-bold uppercase tracking-wider">{role}</p>}
                    {profile?.headline && (
                        <p className="text-[11px] text-slate-500 italic mt-1.5 leading-snug line-clamp-2">
                            “{profile.headline}”
                        </p>
                    )}
                    <div className="flex items-center gap-3 mt-2">
                        {profile?.linkedin_url && <LinkedInLink url={profile.linkedin_url} compact />}
                        {person.email && (
                            <p className="text-[10px] text-slate-400 truncate font-medium opacity-0 group-hover/person:opacity-100 transition-opacity">
                                {person.email}
                            </p>
                        )}
                    </div>
                </div>
            </div>
            {person.context && (
                <div className="relative">
                    <div className="absolute top-0 left-0 w-1 h-full bg-brand/10 rounded-full" />
                    <p className="text-sm text-slate-600 leading-relaxed pl-4 font-medium italic">{person.context}</p>
                </div>
            )}
            {!!researched?.persona?.posts_about?.length && (
                <div className="flex flex-wrap gap-1.5">
                    {researched.persona.posts_about.slice(0, 3).map((theme, i) => (
                        <span
                            key={i}
                            className="px-2 py-0.5 rounded-md bg-white border border-slate-200 text-[10px] font-semibold text-slate-500"
                        >
                            {theme}
                        </span>
                    ))}
                </div>
            )}
        </div>
    );
};

const StakeholderView: React.FC<{
    raw: string;
    intelByEmail: Record<string, AttendeeIntel>;
}> = ({ raw, intelByEmail }) => {
    const parsed = parseStakeholderMap(raw);
    if (parsed.kind === 'text') {
        return (
            <div className="prose prose-slate max-w-none text-slate-600 bg-slate-50 p-6 rounded-2xl border border-slate-100">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{parsed.text}</ReactMarkdown>
            </div>
        );
    }
    if (!parsed.people.length) return <p className="text-slate-400 italic text-sm">No stakeholders listed.</p>;
    return (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            {parsed.people.map((person, i) => (
                <StakeholderCard
                    key={`${person.email || person.name}-${i}`}
                    person={person}
                    intel={intelByEmail[(person.email || '').toLowerCase()]}
                />
            ))}
        </div>
    );
};

const ListBody: React.FC<{ items: string[] }> = ({ items }) => (
    <ul className="space-y-3">
        {items.map((item, idx) => (
            <li key={idx} className="flex gap-3 group">
                <div className="mt-1.5 w-1.5 h-1.5 rounded-full bg-brand/30 group-hover:bg-brand transition-colors shrink-0" />
                <div className="text-slate-600 text-sm leading-relaxed prose prose-sm max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{item}</ReactMarkdown>
                </div>
            </li>
        ))}
        {items.length === 0 && <p className="text-slate-400 italic text-sm">No items generated.</p>}
    </ul>
);

/** Product fit splits on `|` and then the first `:` into label/content. */
const ProductFitBody: React.FC<{ text: string; isInternal: boolean }> = ({ text, isInternal }) => {
    const parts = (text || '')
        .split('|')
        .map(s => s.trim())
        .filter(Boolean);

    if (parts.length > 1) {
        return (
            <div className="space-y-4">
                {parts.map((section, idx) => {
                    const pieces = section.split(':');
                    if (pieces.length >= 2) {
                        const label = pieces[0].replace(/^\*\*|\*\*$/g, '').trim();
                        const content = pieces.slice(1).join(':').trim();
                        return (
                            <div
                                key={idx}
                                className="flex flex-col gap-1 border-b border-slate-100 last:border-0 pb-3 last:pb-0"
                            >
                                <span
                                    className={cn(
                                        'text-[10px] font-bold uppercase tracking-wider',
                                        isInternal ? 'text-brand' : 'text-slate-400',
                                    )}
                                >
                                    {label}
                                </span>
                                <div
                                    className={cn(
                                        'text-sm leading-relaxed prose prose-sm max-w-none',
                                        !isInternal && 'prose-invert',
                                    )}
                                >
                                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
                                </div>
                            </div>
                        );
                    }
                    return (
                        <div
                            key={idx}
                            className={cn('text-sm leading-relaxed prose prose-sm max-w-none', !isInternal && 'prose-invert')}
                        >
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{section}</ReactMarkdown>
                        </div>
                    );
                })}
            </div>
        );
    }

    return (
        <div className={cn('prose prose-sm max-w-none', !isInternal && 'prose-invert')}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </div>
    );
};

// ─── Page state ───────────────────────────────────────────────────────────

/** A loaded brief, stamped with the event it belongs to. */
type Loaded = { forEventId: string; data: MeetingPrepData };
type VersionsState = { forEventId: string; list: VersionSummary[]; latestVersion: number | null };
/** The one write (or version switch) in flight. `regenerate` is only the
 *  quick call that STARTS a background build; the build itself is `Build`. */
type Pending = { kind: 'edit' | 'rewrite'; key: SectionKey } | { kind: 'regenerate' | 'restore' | 'switch' };
/** A background build for this meeting, stamped with the event. */
type Build = { forEventId: string; build: PendingBuild };
/** A Regenerate landed but was not switched to on its own. */
type ReadyNotice = { forEventId: string; runId: string; version: number | null; note: string | null };

const summaryOf = (d: MeetingPrepData): VersionSummary | null =>
    d.run_id && d.version
        ? {
              run_id: d.run_id,
              version: d.version,
              created_at: d.created_at ?? null,
              change_kind: d.change_kind ?? null,
              change_note: d.change_note ?? null,
              changed_section: d.changed_section ?? null,
              base_version: d.base_version ?? null,
              is_latest: d.is_latest !== false,
          }
        : null;

const HEADER_BUTTON =
    'flex items-center justify-center gap-2 px-5 py-3.5 bg-white border border-slate-200 text-slate-700 rounded-2xl hover:bg-slate-50 disabled:opacity-50 transition-all shadow-sm font-bold text-sm whitespace-nowrap';

export const MeetingPrep: React.FC = () => {
    const { eventId = '' } = useParams<{ eventId: string }>();
    const location = useLocation();
    const navEvent = (location.state as { event?: { location?: string; title?: string } } | null)?.event;
    const eventLocation = navEvent?.location;
    const { unread, markRead, expectSoon, acknowledgeMeetingPrep } = useNotifications();

    const [loaded, setLoaded] = useState<Loaded | null>(null);
    const [loadError, setLoadError] = useState<{ forEventId: string; message: string } | null>(null);
    const [versions, setVersions] = useState<VersionsState | null>(null);
    const [pending, setPending] = useState<Pending | null>(null);
    const [docBusy, setDocBusy] = useState<'word' | 'save' | null>(null);
    const [focusOn, setFocusOn] = useState('');
    const [keepEdits, setKeepEdits] = useState(true);
    /** "What You Owe Them" opened from empty, for this event. */
    const [owedOpenFor, setOwedOpenFor] = useState<string | null>(null);
    // WGLL feedback — the caller's settled rating on this meeting's briefing
    // (keyed by event id), loaded so the control shows the right face on reload.
    const [feedback, setFeedback] = useState<{
        forEventId: string;
        rating: FeedbackRating | null;
        comment: string | null;
    } | null>(null);

    /** The background build in flight for this meeting, if any. */
    const [build, setBuild] = useState<Build | null>(null);
    const [readyNotice, setReadyNotice] = useState<ReadyNotice | null>(null);

    /** The event whose responses may still write state. */
    const latest = useRef('');
    /** A write is in flight — guards a double click inside one render. */
    const inFlight = useRef(false);
    /** Builds this page has watched settle: a version list read before one
     *  settled must not bring it back. */
    const settledRuns = useRef<Set<string>>(new Set());
    /** The builds this page follows (run id → its meeting, and how to end the
     *  notifications poll's wait for it once the page has seen it finish).
     *  Kept after the reader moves to another meeting, so the wait outlives
     *  the visit; dropped once the build is known to have settled. */
    const expecting = useRef<Map<string, { eventId: string; end: () => void }>>(new Map());
    /** Sections with an unsaved edit open (read when a build lands, never
     *  rendered — so a ref, not state). */
    const dirtyEditors = useRef<Set<SectionKey>>(new Set());

    /** Follow a build in flight for this meeting (ignores one already
     *  settled, or another meeting's). */
    const trackBuild = useCallback(
        (id: string, b: PendingBuild | null | undefined) => {
            if (!b || settledRuns.current.has(b.run_id) || latest.current !== id) return;
            setBuild({ forEventId: id, build: b });
            if (!expecting.current.has(b.run_id)) {
                // The "ready" notification should land within seconds, not
                // on the next minute's poll — including after the reader has
                // left this page (so leaving does not end the wait).
                expecting.current.set(b.run_id, {
                    eventId: id,
                    end: expectSoon(undefined, n => isMeetingPrepKind(n.kind) && meetingPrepEventId(n.link) === id),
                });
            }
        },
        [expectSoon],
    );

    /** These builds have settled: stop following them (and stop the
     *  notifications poll hurrying for them), and never pick them up again. */
    const forgetBuilds = useCallback((runIds: readonly string[]) => {
        if (!runIds.length) return;
        for (const runId of runIds) {
            settledRuns.current.add(runId);
            expecting.current.get(runId)?.end();
            expecting.current.delete(runId);
        }
        setBuild(b => (b && runIds.includes(b.build.run_id) ? null : b));
    }, []);

    /** Re-read the version list; also returns it (null when it could not be
     *  read, or the reader has moved to another meeting). */
    const refreshVersions = useCallback(
        async (id: string): Promise<VersionList | null> => {
            try {
                const res = await listVersions(id);
                if (latest.current !== id) return null;
                setVersions({ forEventId: id, list: res.versions ?? [], latestVersion: res.latest_version ?? null });
                trackBuild(id, res.pending);
                return res;
            } catch {
                // Non-fatal: the menu falls back to the version on screen.
                return null;
            }
        },
        [trackBuild],
    );

    const load = useCallback(async () => {
        const id = eventId;
        if (!id) return;
        latest.current = id;
        // The builds followed as the request goes out — only these can be
        // ones the answer says have settled (see `staleBuilds`).
        const followed = [...expecting.current].map(([runId, x]) => [runId, x.eventId] as const);
        try {
            const res = await getPrep(id);
            if (latest.current !== id) return;
            setLoadError(null);
            if (res.kind === 'building') {
                // No version yet: Cubie is building v1 in the background.
                trackBuild(id, res.build);
                return;
            }
            // A build followed on an earlier visit that settled while the
            // reader was elsewhere: the brief below already is its result
            // (or newer), so drop it quietly.
            forgetBuilds(staleBuilds(followed, id, res.data.pending_run));
            setLoaded({ forEventId: id, data: res.data });
            // Opening the latest version is seeing it: its "ready"
            // notification has nothing left to say.
            acknowledgeMeetingPrep(id, res.data.created_at);
            trackBuild(id, res.data.pending_run);
            void refreshVersions(id);
        } catch (e) {
            if (latest.current !== id) return;
            setLoadError({ forEventId: id, message: readPrepError(e, 'Failed to load the briefing.').message });
        }
    }, [eventId, refreshVersions, trackBuild, forgetBuilds, acknowledgeMeetingPrep]);

    useEffect(() => {
        void load();
    }, [load]);

    // The caller's prior rating for this briefing (non-fatal on failure — the
    // control just starts unrated).
    useEffect(() => {
        if (!eventId) return;
        let cancelled = false;
        api.get('/api/feedback/mine', { params: { surface: 'meeting_prep' } })
            .then(res => {
                if (cancelled) return;
                const mine = (res.data || {})[eventId];
                setFeedback({
                    forEventId: eventId,
                    rating: (mine?.rating as FeedbackRating) || null,
                    comment: mine?.comment || null,
                });
            })
            .catch(() => {
                /* non-fatal */
            });
        return () => {
            cancelled = true;
        };
    }, [eventId]);

    // ── Derived ──
    const data = loaded && loaded.forEventId === eventId ? loaded.data : null;
    const error = loadError && loadError.forEventId === eventId ? loadError.message : null;
    const buildHere = build && build.forEventId === eventId ? build.build : null;
    const busy = !!eventId && !data && !error && !buildHere;
    const myFeedback = feedback && feedback.forEventId === eventId ? feedback : null;
    // The offer to show a landed version, until it (or something newer) is
    // what is on screen.
    const notice =
        readyNotice &&
        readyNotice.forEventId === eventId &&
        readyNotice.runId !== data?.run_id &&
        !(data?.is_latest !== false && (data?.version ?? 0) >= (readyNotice.version ?? Infinity))
            ? readyNotice
            : null;

    // ── Writes ──

    /** Put a version on screen and refresh the list. False if the reader has
     *  since moved to another meeting. */
    const adopt = useCallback(
        (id: string, next: MeetingPrepData): boolean => {
            if (latest.current !== id) return false;
            setLoaded({ forEventId: id, data: next });
            void refreshVersions(id);
            return true;
        },
        [refreshVersions],
    );

    const showLatest = useCallback(
        async (id: string, runId?: string | null) => {
            try {
                if (runId) {
                    adopt(id, await getVersion(runId));
                    return;
                }
                const res = await getPrep(id);
                if (res.kind === 'ready') adopt(id, res.data);
                else trackBuild(id, res.build);
            } catch (e) {
                if (latest.current === id) toast.error(readPrepError(e, "Couldn't open the latest version.").message);
            }
        },
        [adopt, trackBuild],
    );

    /** A version (`runId`, v`version`) landed for this meeting while
     *  `onScreen` is showing: switch to it, offer it in the banner when the
     *  reader is in the middle of something, or say nothing when it is not
     *  newer than what is on screen. */
    const presentLanded = (
        id: string,
        runId: string,
        version: number | null,
        onScreen: MeetingPrepData,
    ) => {
        const editing = dirtyEditors.current.size > 0;
        const elsewhere = onScreen.is_latest === false;
        const action = readyAction(version, onScreen.version ?? null, editing || elsewhere || inFlight.current);
        if (action === 'quiet') return;
        const label = version ? `v${version} is ready` : 'Your new version is ready';
        if (action === 'show') {
            toast.success(`${label} — showing it now.`);
            setReadyNotice(null);
            void showLatest(id, runId);
            return;
        }
        toast.success(label);
        setReadyNotice({
            forEventId: id,
            runId,
            version,
            note: editing
                ? 'Your unsaved edit stays open until you switch.'
                : elsewhere
                  ? `You're viewing v${onScreen.version}.`
                  : null,
        });
    };

    // ── The background build ──

    /** Still going: keep the pill (queued → building) current. */
    const onBuildProgress = useEffectEvent((s: BuildStatus) => {
        if (s.status !== 'queued' && s.status !== 'running') return;
        const status = s.status;
        setBuild(b =>
            b && b.build.run_id === s.run_id && b.build.status !== status
                ? { ...b, build: { ...b.build, status, started_at: s.started_at ?? b.build.started_at } }
                : b,
        );
    });

    /** Ready or failed. The first version opens like any visit; a new
     *  version replaces the one on screen unless the reader is in the middle
     *  of something there — then the banner offers it. */
    const onBuildSettled = useEffectEvent((s: BuildStatus) => {
        const id = eventId;
        if (!id || latest.current !== id) return;
        // The reader saw the result here: its notification is read, and the
        // notifications poll need not hurry for it.
        forgetBuilds([s.run_id]);
        acknowledgeMeetingPrep(id, s.finished_at);
        const onScreen = loaded && loaded.forEventId === id ? loaded.data : null;

        if (s.status === 'failed') {
            if (onScreen) toast.error(s.detail || REGENERATE_FAILED);
            else setLoadError({ forEventId: id, message: s.detail || FIRST_BUILD_FAILED });
            return;
        }
        if (!onScreen) {
            void load();
            return;
        }
        void refreshVersions(id);
        presentLanded(id, s.run_id, s.version, onScreen);
    });

    /** The status call says the build is not there (404) or not theirs. */
    const onBuildLost = useEffectEvent((runId: string) => {
        const id = eventId;
        if (!id || latest.current !== id) return;
        forgetBuilds([runId]);
        const onScreen = loaded && loaded.forEventId === id ? loaded.data : null;
        if (onScreen) toast.error(BUILD_LOST);
        else setLoadError({ forEventId: id, message: BUILD_LOST });
    });

    // Poll the build in flight until it settles. Keyed on the build only:
    // leaving the meeting (or the page) stops it, and a network blip backs
    // off rather than ending the wait.
    const buildRunId = buildHere?.run_id ?? null;
    useEffect(() => {
        if (!buildRunId) return;
        let cancelled = false;
        let timer: number | undefined;
        let failures = 0;
        const tick = async () => {
            try {
                const s = await getBuildStatus(buildRunId);
                if (cancelled) return;
                failures = 0;
                if (isSettled(s)) {
                    onBuildSettled(s);
                    return;
                }
                onBuildProgress(s);
            } catch (e) {
                if (cancelled) return;
                const status = readPrepError(e, BUILD_LOST).status;
                if (status === 404 || status === 403) {
                    onBuildLost(buildRunId);
                    return;
                }
                failures += 1;
            }
            if (!cancelled) timer = window.setTimeout(() => void tick(), nextPollDelay(failures));
        };
        timer = window.setTimeout(() => void tick(), nextPollDelay(0));
        return () => {
            cancelled = true;
            window.clearTimeout(timer);
        };
    }, [buildRunId]);

    // A meeting-prep notification for THIS meeting that the page did not
    // acknowledge — a build it was not watching (another tab's, or one that
    // settled while the reader was elsewhere). The toaster leaves it to this
    // page, so catch up: show (or offer) a newer version, say so if it failed,
    // and mark it read. While a build of this meeting is followed, or the
    // brief is still opening, the page acknowledges it on its own.
    const prepNotices = unackedPrepNotices(unread, eventId);
    const prepNoticeIds =
        prepNoticeStep(prepNotices, { following: !!buildHere, opening: !data && !error }) === 'refresh'
            ? prepNotices.map(n => n.id).join('\n')
            : '';
    const onPrepNotices = useEffectEvent(async (ids: string[]) => {
        const id = eventId;
        if (!id || latest.current !== id) return;
        const rows = unread.filter(n => ids.includes(n.id));
        ids.forEach(n => void markRead(n));
        const onScreen = loaded && loaded.forEventId === id ? loaded.data : null;
        if (!onScreen) {
            // Nothing on screen (the last open failed): open it again.
            void load();
            return;
        }
        // Newer than any version the page already knew of — not merely newer
        // than an older version the reader chose to open.
        const known = Math.max(
            onScreen.version ?? 0,
            onScreen.latest_version ?? 0,
            versions && versions.forEventId === id ? (versions.latestVersion ?? 0) : 0,
        );
        const newer = newerVersion(await refreshVersions(id), known);
        if (latest.current !== id) return;
        if (newer) {
            presentLanded(id, newer.run_id, newer.version, onScreen);
            return;
        }
        const failed = rows.find(n => n.kind === MEETING_PREP_FAILED);
        if (failed) toast.error(failed.body || REGENERATE_FAILED);
    });
    useEffect(() => {
        if (!prepNoticeIds) return;
        void onPrepNotices(prepNoticeIds.split('\n'));
    }, [prepNoticeIds]);

    /** After a write the browser stopped waiting for. The server may not have
     *  stopped — a rewrite can still save a new version — so never say
     *  nothing changed: re-read the list and show a version newer than `from`
     *  if one has already landed. */
    const showIfNewer = async (id: string, from: number | null) => {
        const newer = newerVersion(await refreshVersions(id), from);
        if (latest.current !== id) return;
        if (newer) {
            toast.info(`A new version (v${newer.version}) was saved — showing it now.`);
            await showLatest(id, newer.run_id);
        } else {
            toast.info(STILL_WORKING_MESSAGE);
        }
    };

    /** One write at a time; the new version replaces what is on screen. On
     *  failure the current version stays; on 409 the newer one is loaded; on a
     *  client timeout the page looks for the version the server may have
     *  saved anyway. */
    const runWrite = async (
        p: Pending,
        call: () => Promise<MeetingPrepData>,
        fallback: string,
        success: (next: MeetingPrepData) => string,
    ): Promise<boolean> => {
        const id = eventId;
        if (!id || inFlight.current) return false;
        const from = data?.version ?? null;
        inFlight.current = true;
        setPending(p);
        try {
            const next = await call();
            const outcome = writeOutcome(next);
            if (outcome !== 'saved') {
                // An unsaved brief (no run id or version) would replace the
                // saved one with something that has no version, no Word and
                // nothing to edit: keep the saved one on screen.
                if (latest.current === id) toast.error(outcome === 'unsaved' ? UNSAVED_MESSAGE : fallback);
                return false;
            }
            if (adopt(id, next)) toast.success(success(next));
            return true;
        } catch (e) {
            if (latest.current !== id) return false;
            const err = readPrepError(e, fallback);
            if (isStaleVersion(err)) {
                toast.info(
                    err.latestVersion
                        ? `A newer version exists (v${err.latestVersion}) — showing it now.`
                        : 'A newer version exists — showing it now.',
                );
                await showLatest(id, err.latestRunId);
            } else if (err.timedOut) {
                await showIfNewer(id, from);
            } else {
                toast.error(err.message);
            }
            return false;
        } finally {
            inFlight.current = false;
            setPending(null);
        }
    };

    /** Open a version (read-only unless it is the latest). */
    const viewVersion = async (v: VersionSummary) => {
        const id = eventId;
        if (!id || !data || v.run_id === data.run_id || inFlight.current) return;
        inFlight.current = true;
        setPending({ kind: 'switch' });
        try {
            const next = await getVersion(v.run_id);
            if (latest.current === id) setLoaded({ forEventId: id, data: next });
        } catch (e) {
            if (latest.current === id) toast.error(readPrepError(e, `Couldn't open v${v.version}.`).message);
        } finally {
            inFlight.current = false;
            setPending(null);
        }
    };

    if (!eventId) {
        return (
            <div className="max-w-[1600px] mx-auto px-4 py-8">
                <BackButton fallback="/productivity?tab=calendar" />
                <p className="mt-8 text-center text-slate-500">No meeting selected.</p>
            </div>
        );
    }

    if (!data && buildHere) {
        return (
            <div className="max-w-[1600px] mx-auto px-4 py-8">
                <BackButton fallback="/productivity?tab=calendar" />
                <BuildingPanel
                    title={buildHere.title || navEvent?.title || null}
                    status={buildHere.status}
                    startedAt={buildHere.started_at}
                />
            </div>
        );
    }

    if (busy) {
        return (
            <div className="max-w-[1600px] mx-auto px-4 py-8">
                <div className="mb-6">
                    <BackButton fallback="/productivity?tab=calendar" />
                </div>
                <div role="status" className="flex flex-col items-center justify-center min-h-[50vh] space-y-4 text-center">
                    <Loader2 className="w-10 h-10 text-brand animate-spin" />
                    <h2 className="text-xl font-bold text-slate-800">Opening your briefing…</h2>
                </div>
            </div>
        );
    }

    if (!data) {
        return (
            <div className="max-w-[1600px] mx-auto px-4 py-8">
                <div className="mb-6">
                    <BackButton fallback="/productivity?tab=calendar" />
                </div>
                <div className="flex flex-col items-center justify-center min-h-[50vh] space-y-4 p-8 text-center">
                    <AlertCircle className="w-16 h-16 text-red-500" />
                    <h2 className="text-2xl font-bold text-slate-800">Briefing Interrupted</h2>
                    <p className="text-slate-600 max-w-md">{error}</p>
                    <button
                        onClick={() => {
                            setLoadError(null);
                            void load();
                        }}
                        className="px-6 py-2 bg-brand text-white rounded-lg hover:bg-brand-hover transition-colors"
                    >
                        Try Again
                    </button>
                </div>
            </div>
        );
    }

    // ── The brief on screen ──
    const runId = data.run_id || null;
    const isInternal = !!data.is_internal;
    const isLatest = data.is_latest !== false;
    const canEdit = !!runId && !!data.version && isLatest && !data.generation_failed;
    const locked = pending !== null;

    // The version list, with the version on screen folded in if the list has
    // not caught up with it yet (or could not be read).
    const serverList = versions && versions.forEventId === eventId ? versions.list : [];
    const mine = summaryOf(data);
    const merged = mine && !serverList.some(v => v.run_id === mine.run_id) ? [...serverList, mine] : serverList;
    const latestVersion =
        Math.max(
            data.latest_version ?? 0,
            versions && versions.forEventId === eventId ? (versions.latestVersion ?? 0) : 0,
            ...merged.map(v => v.version),
        ) || null;
    const versionList = [...merged]
        .sort((a, b) => b.version - a.version)
        .map(v => ({ ...v, is_latest: v.version === latestVersion }));

    const editedKeys = isLatest ? editedSectionKeys(data.section_meta) : [];
    const versionWhen = formatVersionWhen(data.created_at, undefined, '');
    const versionLine = data.version
        ? [
              versionWhen ? `${isGeneration(data.change_kind) ? 'Generated' : 'Saved'} ${versionWhen}` : null,
              describeChange(data, isInternal),
          ]
              .filter(Boolean)
              .join(' · ')
        : null;

    const attendeeIntel = data.attendee_intel || [];
    const intelByEmail = indexIntelByEmail(attendeeIntel);

    const saveSection = (key: SectionKey, value: SectionValue) => {
        if (!runId) return Promise.resolve(false);
        return runWrite(
            { kind: 'edit', key },
            () => editSection(runId, key, value),
            `Couldn't save your edit to ${sectionLabel(key, isInternal)}.`,
            // Saving what is already there is not a new version server-side.
            next => (next.version === data.version ? 'Nothing changed — still v' + next.version : `Saved as v${next.version}`),
        );
    };

    const rewriteOne = (key: SectionKey, instruction: string) => {
        if (!runId) return Promise.resolve(false);
        const label = sectionLabel(key, isInternal);
        return runWrite(
            { kind: 'rewrite', key },
            () => rewriteSection(runId, key, instruction),
            `Cubie couldn't rewrite ${label}. Your current version is unchanged.`,
            next => `Cubie rewrote ${label} — saved as v${next.version}`,
        );
    };

    /** Start a new version building in the background. Answers at once; the
     *  banner and the poll take it from there. */
    const doRegenerate = async () => {
        const id = eventId;
        if (!id || inFlight.current || buildHere) return;
        inFlight.current = true;
        setPending({ kind: 'regenerate' });
        try {
            const started = await regenerate({ eventId: id, focusOn, keepEdits: editedKeys.length ? keepEdits : true });
            if (latest.current !== id) return;
            setReadyNotice(null);
            trackBuild(id, started);
        } catch (e) {
            if (latest.current === id) {
                toast.error(
                    readPrepError(e, "Cubie couldn't start a new version. Your current version is unchanged.").message,
                );
            }
        } finally {
            inFlight.current = false;
            setPending(null);
        }
    };

    const showNotice = () => {
        if (!notice) return;
        setReadyNotice(null);
        void showLatest(eventId, notice.runId);
    };

    const doRestore = () => {
        if (!runId) return;
        const from = data.version;
        void runWrite(
            { kind: 'restore' },
            () => restoreVersion(runId),
            "Couldn't restore this version.",
            next =>
                next.version === from ? `v${from} is already the latest` : `Restored v${from} — saved as v${next.version}`,
        );
    };

    const backToLatest = () => {
        const target = versionList.find(v => v.is_latest);
        if (target) void viewVersion(target);
        else void showLatest(eventId);
    };

    const downloadWord = async () => {
        if (!runId) return;
        setDocBusy('word');
        try {
            downloadBlob(await downloadDocx(runId), docxFilename(data.title, data.version));
        } catch {
            toast.error('Word download failed — please try again.');
        } finally {
            setDocBusy(null);
        }
    };

    const saveToAssets = async () => {
        if (!runId) return;
        setDocBusy('save');
        try {
            const res = await saveToAssetsApi(runId);
            if (res?.created) {
                toast.success(
                    `${data.version ? `v${data.version} saved` : 'Saved'} to your Assets page — share it with a teammate from there.`,
                );
            } else {
                toast.info('Already on your Assets page.');
            }
        } catch (e) {
            toast.error(readPrepError(e, 'Save to Assets failed — please try again.').message);
        } finally {
            setDocBusy(null);
        }
    };

    const printPdf = () => {
        // Browsers name the saved PDF after document.title (otherwise "Cubie");
        // swap in the meeting name (and version) for the print, then restore.
        const prev = document.title;
        document.title = `${data.title || prev}${data.version ? ` (v${data.version})` : ''}`;
        window.addEventListener(
            'afterprint',
            () => {
                document.title = prev;
            },
            { once: true },
        );
        window.print();
    };

    /** The props every editable section shares. */
    const sectionProps = (key: SectionKey) => ({
        sectionKey: key,
        title: sectionLabel(key, isInternal),
        runId,
        version: data.version ?? null,
        meta: data.section_meta?.[key] ?? null,
        canEdit,
        locked,
        saving: pending?.kind === 'edit' && pending.key === key,
        rewriting: pending?.kind === 'rewrite' && pending.key === key,
        onSave: (value: SectionValue) => saveSection(key, value),
        onRewrite: (instruction: string) => rewriteOne(key, instruction),
        onDirtyChange: (dirty: boolean) => {
            if (dirty) dirtyEditors.current.add(key);
            else dirtyEditors.current.delete(key);
        },
    });

    const owed = data.open_items_you_owe ?? [];
    const owedEditorOpen = canEdit && owedOpenFor === eventId;
    const objections = data.likely_objections || '';

    return (
        <div className="max-w-[1600px] mx-auto px-4 py-8 space-y-8">
            {/* Back — the app chrome has no global back/breadcrumb, and this page is
                reached by clicking into a meeting from the Productivity calendar or
                the Daily Briefing, so it needs its own way back. Hidden in print. */}
            <div className="no-print">
                <BackButton fallback="/productivity?tab=calendar" />
            </div>

            {/* Header */}
            <header data-tour="meeting-prep-header" className="bg-white/70 backdrop-blur-xl rounded-[2rem] border border-slate-200/60 p-8 shadow-2xl shadow-slate-200/50 flex flex-col lg:flex-row justify-between items-start lg:items-center gap-8 relative z-20 group">
                {/* The accent bar is clipped by its own layer, so the header itself
                    can stay overflow-visible for the version menu. */}
                <div className="absolute inset-0 rounded-[2rem] overflow-hidden pointer-events-none" aria-hidden>
                    <div className="absolute top-0 left-0 w-2 h-full bg-brand" />
                </div>
                <div className="space-y-3 relative z-20 min-w-0">
                    <div className="flex items-center gap-2.5 text-brand text-[11px] font-bold tracking-[0.2em] uppercase">
                        <div className="w-8 h-[1px] bg-brand/30" />
                        <Sparkles className="w-3.5 h-3.5" />
                        {isInternal ? 'Internal Strategy Briefing' : 'Sales Intelligence Briefing'}
                    </div>
                    <h1 className="text-4xl font-black text-slate-900 leading-[1.1] tracking-tight max-w-2xl">{data.title}</h1>
                    <div className="flex flex-wrap items-center gap-3 pt-1">
                        <span className="flex items-center gap-2 bg-slate-100 text-slate-600 px-4 py-1.5 rounded-full text-xs font-bold shadow-sm border border-slate-200/50">
                            <Calendar className="w-3.5 h-3.5 text-brand" />
                            {isInternal ? 'Strategy Sync' : 'High-Stakes Discovery'}
                        </span>
                        {data.meeting_time && (
                            <span className="flex items-center gap-2 bg-slate-100 text-slate-600 px-4 py-1.5 rounded-full text-xs font-bold shadow-sm border border-slate-200/50">
                                <Clock className="w-3.5 h-3.5 text-brand" />
                                {data.meeting_time}
                            </span>
                        )}
                        {eventLocation && (
                            <span className="flex items-center gap-2 bg-slate-100 text-slate-600 px-4 py-1.5 rounded-full text-xs font-bold shadow-sm border border-slate-200/50">
                                <MapPin className="w-3.5 h-3.5 text-brand" />
                                {eventLocation}
                            </span>
                        )}
                    </div>
                    {data.version ? (
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                            <VersionMenu
                                versions={versionList}
                                currentRunId={runId}
                                currentVersion={data.version}
                                latestVersion={latestVersion}
                                isInternal={isInternal}
                                busy={pending?.kind === 'switch'}
                                disabled={locked && pending?.kind !== 'switch'}
                                onSelect={v => void viewVersion(v)}
                            />
                            <span className="hidden print:inline text-xs font-bold text-slate-600">
                                Version {data.version}
                            </span>
                            {versionLine && <p className="text-xs font-medium text-slate-500">{versionLine}</p>}
                        </div>
                    ) : null}
                    <BuiltFromStrip signals={data.signals_used} />
                </div>

                {/* Controls — must stay the header's LAST child: the print CSS hides it. */}
                <div className="flex flex-col gap-3 w-full lg:w-auto lg:min-w-[30rem] lg:max-w-[36rem] relative z-10">
                    {/* The version on screen: print, Word, Assets. */}
                    <div data-tour="meeting-prep-export" className="flex flex-wrap gap-2 lg:justify-end">
                        <button onClick={printPdf} className={HEADER_BUTTON} title="Print or save this version as a PDF">
                            <FileText className="w-4 h-4" />
                            PDF
                        </button>
                        {runId && (
                            <>
                                <button
                                    onClick={() => void downloadWord()}
                                    disabled={docBusy !== null}
                                    title="Download this version as an editable Word document"
                                    className={HEADER_BUTTON}
                                >
                                    {docBusy === 'word' ? (
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                    ) : (
                                        <FileDown className="w-4 h-4" />
                                    )}
                                    Word
                                </button>
                                <button
                                    onClick={() => void saveToAssets()}
                                    disabled={docBusy !== null}
                                    title="Save this version to your Assets page to share it with teammates"
                                    className={HEADER_BUTTON}
                                >
                                    {docBusy === 'save' ? (
                                        <Loader2 className="w-4 h-4 animate-spin" />
                                    ) : (
                                        <FolderPlus className="w-4 h-4" />
                                    )}
                                    Save to Assets
                                </button>
                            </>
                        )}
                    </div>
                    {/* A new version: the intent steers it. Latest only. */}
                    {isLatest && (
                        <div data-tour="meeting-prep-regenerate" className="flex flex-col sm:flex-row gap-2">
                            <div className="flex-1 min-w-0 sm:min-w-[16rem] relative group/input">
                                <input
                                    type="text"
                                    aria-label="Meeting intent"
                                    placeholder="Meeting intent (e.g. 'Push for pilot approval')"
                                    value={focusOn}
                                    onChange={e => setFocusOn(e.target.value)}
                                    onKeyDown={e => {
                                        if (e.key === 'Enter' && !locked && !buildHere) {
                                            e.preventDefault();
                                            void doRegenerate();
                                        }
                                    }}
                                    className="w-full pl-11 pr-4 py-3.5 bg-slate-50/50 border border-slate-200 rounded-2xl text-sm font-medium focus:ring-4 focus:ring-brand/5 focus:border-brand transition-all outline-none"
                                />
                                <Target className="w-4 h-4 text-slate-400 absolute left-4 top-1/2 -translate-y-1/2 group-focus-within/input:text-brand transition-colors" />
                            </div>
                            <button
                                onClick={() => void doRegenerate()}
                                disabled={locked || !!buildHere}
                                title={
                                    buildHere
                                        ? 'A new version is already being built in the background'
                                        : "Build a new version from today's signals (and the meeting intent, if you set one)"
                                }
                                className="flex items-center justify-center gap-2 px-7 py-3.5 bg-slate-900 text-white rounded-2xl hover:bg-slate-800 disabled:opacity-50 transition-all shadow-xl shadow-slate-900/10 font-bold text-sm group/btn whitespace-nowrap"
                            >
                                {pending?.kind === 'regenerate' || buildHere ? (
                                    <Loader2 className="w-4 h-4 animate-spin" />
                                ) : (
                                    <RefreshCw className="w-4 h-4 group-hover/btn:rotate-180 transition-transform duration-500" />
                                )}
                                {pending?.kind === 'regenerate' ? 'Starting…' : buildHere ? 'Building…' : 'Regenerate'}
                            </button>
                        </div>
                    )}
                    {isLatest && editedKeys.length > 0 && (
                        <label
                            className="flex items-center gap-2 self-end text-xs font-semibold text-slate-600 cursor-pointer select-none"
                            title={`Sections you edited by hand: ${editedKeys
                                .map(k => sectionLabel(k, isInternal))
                                .join(', ')}. Ticked, they are carried into the new version unchanged.`}
                        >
                            <input
                                type="checkbox"
                                checked={keepEdits}
                                onChange={e => setKeepEdits(e.target.checked)}
                                className="h-4 w-4 rounded border-slate-300 text-brand focus:ring-brand/30"
                            />
                            Keep my edits ({editedKeys.length}) when regenerating
                        </label>
                    )}
                </div>
            </header>

            {!isLatest && (
                <div className="no-print flex flex-wrap items-center gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-5 py-3 text-sm text-amber-900">
                    <History className="w-4 h-4 shrink-0 text-amber-700" />
                    <span className="min-w-0">
                        You're viewing <strong>v{data.version}</strong>
                        {versionWhen ? ` from ${versionWhen}` : ''} — read-only.
                        {latestVersion ? ` The latest is v${latestVersion}.` : ''}
                    </span>
                    <div className="ml-auto flex flex-wrap gap-2">
                        <button
                            type="button"
                            onClick={doRestore}
                            disabled={locked}
                            title="Make this version the latest again. Nothing is lost — it is saved as a new version."
                            className="inline-flex items-center gap-1.5 rounded-xl bg-brand px-3.5 py-2 text-xs font-bold text-white transition-colors hover:bg-brand-hover disabled:opacity-50"
                        >
                            {pending?.kind === 'restore' ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                                <RotateCcw className="w-3.5 h-3.5" />
                            )}
                            Restore this version
                        </button>
                        <button
                            type="button"
                            onClick={backToLatest}
                            disabled={locked}
                            className="inline-flex items-center gap-1.5 rounded-xl border border-amber-300 bg-white px-3.5 py-2 text-xs font-bold text-amber-900 transition-colors hover:bg-amber-100 disabled:opacity-50"
                        >
                            Back to latest
                        </button>
                    </div>
                </div>
            )}

            {buildHere ? (
                <BuildBanner state="building" status={buildHere.status} startedAt={buildHere.started_at} />
            ) : notice ? (
                <BuildBanner
                    state="ready"
                    version={notice.version}
                    note={notice.note}
                    onShow={showNotice}
                    onDismiss={() => setReadyNotice(null)}
                />
            ) : null}

            <style
                dangerouslySetInnerHTML={{
                    __html: `
        @media print {
          /* Hide non-essential UI */
          @page { margin: 1cm; }
          .no-print, button, input, textarea, header > div:last-child, aside, nav { display: none !important; }

          html, body, #root, div.flex.h-screen, main {
            height: auto !important;
            overflow: visible !important;
            display: block !important;
            position: static !important;
          }

          body { background: white !important; padding: 0 !important; margin: 0 !important; }
          .max-w-[1600px] { max-width: 100% !important; padding: 0 !important; margin: 0 !important; width: 100% !important; }

          .rounded-2xl { border-radius: 8px !important; }
          .shadow-sm, .shadow-md, .shadow-lg, .shadow-xl { box-shadow: none !important; }
          .border-slate-200 { border-color: #e2e8f0 !important; }
          .bg-slate-900 { background: #f8fafc !important; color: #0f172a !important; border: 1px solid #e2e8f0 !important; }
          .text-white { color: #0f172a !important; }
          .prose-invert { color: #334155 !important; }
          .prose-invert * { color: #334155 !important; }
          .SectionCard {
            page-break-inside: avoid !important;
            break-inside: avoid !important;
            border: 1px solid #e2e8f0 !important;
            margin-bottom: 20px !important;
            display: block !important;
          }
          header { border: none !important; margin-bottom: 2rem !important; }
        }
      `,
                }}
            />

            {/* Main Content Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
                {/* Left Column: Core Analysis */}
                <div className="lg:col-span-8 space-y-8">
                    <EditableSection
                        {...sectionProps('executive_summary')}
                        icon={Sparkles}
                        value={data.executive_summary}
                        className="border-l-4 border-l-brand"
                    >
                        <div className="prose prose-slate max-w-none">
                            <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.executive_summary}</ReactMarkdown>
                        </div>
                    </EditableSection>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                        <EditableSection {...sectionProps('meeting_objective')} icon={Target} value={data.meeting_objective}>
                            <ProseField content={data.meeting_objective} />
                        </EditableSection>
                        <EditableSection {...sectionProps('client_profile')} icon={Users} value={data.client_profile}>
                            <ProseField content={data.client_profile} />
                        </EditableSection>
                    </div>

                    <EditableSection
                        {...sectionProps('stakeholder_map')}
                        icon={UserPlus}
                        value={data.stakeholder_map}
                        className="overflow-hidden"
                    >
                        <StakeholderView raw={data.stakeholder_map} intelByEmail={intelByEmail} />
                    </EditableSection>

                    {/* UC-47 — the people, researched: position, what they publish, and how
                        to talk to them. External attendees only; rendered from the research
                        step's own output (with its citations) rather than anything the
                        briefing model wrote, so it is never editable. */}
                    {!!attendeeIntel.length && (
                        <SectionCard icon={Radar} title="Attendee Intelligence" className="overflow-hidden">
                            <div className="space-y-4">
                                <p className="text-xs text-slate-400 leading-relaxed">
                                    Public professional research on the external attendees — position, what they have
                                    been posting about, and a read on their style. Everything is linked; the style read
                                    is labelled with how much evidence sits behind it.
                                </p>
                                <AttendeeIntelPanel intel={attendeeIntel} />
                            </div>
                        </SectionCard>
                    )}

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                        <EditableSection
                            {...sectionProps('recent_interactions')}
                            icon={MessageSquare}
                            value={data.recent_interactions}
                        >
                            <ProseField content={data.recent_interactions} size="sm" />
                        </EditableSection>
                        <EditableSection
                            {...sectionProps('past_meeting_context')}
                            icon={History}
                            value={data.past_meeting_context}
                        >
                            <ProseField content={data.past_meeting_context} size="sm" />
                        </EditableSection>
                    </div>

                    <EditableSection
                        {...sectionProps('recommended_positioning')}
                        icon={Briefcase}
                        value={data.recommended_positioning}
                        className="bg-gradient-to-br from-brand/5 to-brand-hover/5 border-brand/10"
                        footer={
                            objections.trim() || canEdit ? (
                                <EditableSection
                                    {...sectionProps('likely_objections')}
                                    icon={ShieldAlert}
                                    value={objections}
                                    variant="inset"
                                    className={objections.trim() ? undefined : 'no-print'}
                                >
                                    {objections.trim() ? (
                                        <ObjectionsList content={objections} />
                                    ) : (
                                        <p className="text-slate-400 italic text-sm">None listed yet.</p>
                                    )}
                                </EditableSection>
                            ) : null
                        }
                    >
                        <ProseField content={data.recommended_positioning} />
                    </EditableSection>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                        <EditableSection
                            {...sectionProps('suggested_agenda')}
                            icon={ClipboardList}
                            value={data.suggested_agenda}
                        >
                            <ListBody items={data.suggested_agenda} />
                        </EditableSection>
                        <EditableSection {...sectionProps('talking_points')} icon={Mic} value={data.talking_points}>
                            <ListBody items={data.talking_points} />
                        </EditableSection>
                    </div>

                    {/* Only when the brief found something outstanding (an empty
                        "you owe nothing" card is noise) — or when the reader is
                        adding to it. */}
                    {owed.length > 0 || owedEditorOpen ? (
                        <EditableSection
                            {...sectionProps('open_items_you_owe')}
                            icon={MailWarning}
                            value={owed}
                            defaultEditing={owed.length === 0}
                            onEditingChange={editing => {
                                if (!editing) setOwedOpenFor(null);
                            }}
                        >
                            <ListBody items={owed} />
                        </EditableSection>
                    ) : canEdit ? (
                        <button
                            type="button"
                            onClick={() => setOwedOpenFor(eventId)}
                            className="no-print flex w-full items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-300 px-4 py-3 text-xs font-bold text-slate-500 transition-colors hover:border-brand/40 hover:text-brand"
                        >
                            <Plus className="w-4 h-4" />
                            Add “What You Owe Them”
                        </button>
                    ) : null}

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                        <EditableSection
                            {...sectionProps('discovery_questions')}
                            icon={Search}
                            value={data.discovery_questions}
                        >
                            <ListBody items={data.discovery_questions} />
                        </EditableSection>
                        <EditableSection {...sectionProps('next_steps')} icon={ArrowRight} value={data.next_steps}>
                            <ListBody items={data.next_steps} />
                        </EditableSection>
                    </div>
                </div>

                {/* Right Column: Side Info */}
                <aside className="lg:col-span-4 space-y-8">
                    <AccountSnapshotCard snapshot={data.account_snapshot} />

                    <EditableSection
                        {...sectionProps('product_fit_analysis')}
                        icon={BookOpen}
                        value={data.product_fit_analysis}
                        dark={!isInternal}
                        className={cn(
                            'border-none shadow-xl shadow-slate-200',
                            isInternal ? 'bg-white text-slate-900 border border-slate-100' : 'bg-slate-900 text-white',
                        )}
                        footer={
                            data.recommended_products.length > 0 ? (
                                <div className="space-y-3 mt-6">
                                    <h4 className="text-xs font-bold text-slate-400 uppercase tracking-widest">
                                        Recommended Products
                                    </h4>
                                    <div className="flex flex-wrap gap-2">
                                        {data.recommended_products.map((p, idx) => (
                                            <span
                                                key={idx}
                                                className="px-3 py-1 bg-white/10 border border-white/20 rounded-lg text-sm font-medium"
                                            >
                                                {p}
                                            </span>
                                        ))}
                                    </div>
                                </div>
                            ) : null
                        }
                    >
                        <div className={cn('space-y-4', !isInternal && 'text-white')}>
                            <ProductFitBody text={data.product_fit_analysis} isInternal={isInternal} />
                        </div>
                    </EditableSection>

                    {/* Evidence */}
                    <SectionCard icon={ShieldAlert} title="Source Evidence" className="bg-white border-slate-200 shadow-sm">
                        <div className="space-y-4">
                            <p className="text-xs text-slate-400 uppercase font-bold tracking-wider">
                                Citations & References
                            </p>
                            <div className="space-y-3">
                                {data.source_evidence.map((evidence, idx) => (
                                    <div
                                        key={idx}
                                        className="flex gap-3 p-3 bg-slate-50 rounded-xl border border-slate-100 text-sm group cursor-default hover:border-brand/20 transition-colors"
                                    >
                                        <div className="mt-1">
                                            <ExternalLink className="w-3.5 h-3.5 text-slate-400 group-hover:text-brand transition-colors" />
                                        </div>
                                        <span className="text-slate-600 leading-snug">{evidence}</span>
                                    </div>
                                ))}
                                {data.source_evidence.length === 0 && (
                                    <p className="text-slate-400 italic text-sm text-center py-4">No specific sources cited.</p>
                                )}
                            </div>
                        </div>
                    </SectionCard>

                    {/* Errors/Warnings */}
                    {data.partial_errors.length > 0 && (
                        <div className="p-4 bg-amber-50 rounded-2xl border border-amber-100 space-y-2">
                            <div className="flex items-center gap-2 text-amber-700 font-bold text-sm">
                                <AlertCircle className="w-4 h-4" />
                                Intelligence Gaps
                            </div>
                            <ul className="text-xs text-amber-600 space-y-1 list-disc pl-4">
                                {data.partial_errors.map((err, idx) => (
                                    <li key={idx}>{err}</li>
                                ))}
                            </ul>
                        </div>
                    )}
                </aside>
            </div>

            {/* Feedback — rate the whole briefing (trains Cubie; hidden in print/PDF) */}
            <div className="no-print flex flex-col items-center gap-3 pt-6 mt-2 border-t border-slate-200/60">
                <p className="text-sm font-semibold text-slate-500">Was this briefing helpful?</p>
                <FeedbackButtons
                    surface="meeting_prep"
                    proposalId={eventId}
                    proposalText={[data.title, data.executive_summary, data.meeting_objective].filter(Boolean).join('\n\n')}
                    proposalMetadata={{
                        title: data.title,
                        is_internal: data.is_internal,
                        is_sales_user: data.is_sales_user,
                        event_id: eventId,
                        version: data.version ?? undefined,
                        run_id: runId ?? undefined,
                        focus_on: focusOn || undefined,
                        origin: 'meeting_prep',
                    }}
                    initialRating={myFeedback?.rating ?? null}
                    initialComment={myFeedback?.comment ?? null}
                    hideLabel
                />
            </div>
        </div>
    );
};
