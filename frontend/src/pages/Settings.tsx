import React, { useEffect, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { Save, Sparkles, Loader2, CheckCircle2, AlertCircle, Link2, Unlink, RotateCcw, X } from 'lucide-react';
import client from '../api/client';
import { formatInstant, formatInstantDate } from '../utils/datetime';
import { ScheduledTasksTab } from './settings/ScheduledTasksTab';
import { GmailAccountsCard } from './settings/GmailAccountsCard';
import { CubieForChromeCard } from './settings/CubieForChromeCard';
import { useAuth } from '../context/AuthContext';
import { VoiceShortcutSetting } from '../components/voice/VoiceShortcutSetting';
import { PageToursSetting } from '../tours/PageToursSetting';
import { VOICE_SHORTCUT_EVENT } from '../components/voice/shortcut';
import type { VoiceShortcutPref } from '../components/voice/shortcut';

type Tone = 'formal' | 'conversational' | 'direct';

interface Preferences {
    writing_style: string;
    tone: Tone | '';
    preferred_greeting: string;
    additional_instructions: string;
}

const EMPTY: Preferences = {
    writing_style: '',
    tone: '',
    preferred_greeting: '',
    additional_instructions: '',
};

interface PreviewResult {
    subject: string | null;
    body: string | null;
    raw_response: string;
}

type TabKey = 'general' | 'ai' | 'email' | 'slack' | 'scheduled-tasks';

/**
 * The Settings sections. Their NAVIGATION lives in the app's left panel now:
 * Layout.tsx renders one entry per section pointing at /settings?tab=<key>, so
 * this page no longer carries its own section list — two navigations an inch
 * apart is worse than one.
 *
 * What's New's settings (the morning post's Slack channel, the tease) used to
 * be a tab here. They apply to everyone, not to the reader, so they moved to
 * Admin → What's New. An old `?tab=whats-new` link sends an
 * admin there; anyone else gets General, as for any unknown tab.
 */
const TAB_KEYS: TabKey[] = ['general', 'ai', 'email', 'slack', 'scheduled-tasks'];

const isTabKey = (value: string | null): value is TabKey =>
    value !== null && (TAB_KEYS as string[]).includes(value);

export const Settings: React.FC = () => {
    // DERIVED FROM THE URL, not held in state. The section links live outside
    // this component now, so an external navigation is the source of truth.
    // Reading ?tab= into useState once (as this did) meant a click in the left
    // panel changed the URL and left the page rendering the previous section.
    const [searchParams] = useSearchParams();
    const { isAdmin, user, token } = useAuth();
    const identity = `${user?.workspace?.slug ?? ''}:${user?.email ?? ''}:${token ?? ''}`;
    const requestedTab = searchParams.get('tab');
    const activeTab: TabKey = isTabKey(requestedTab) ? requestedTab : 'general';

    // What's New moved to Admin: an admin's old link lands there. Derived from
    // the URL on every render, so it also fires once /api/users/me has
    // answered and `isAdmin` turns true.
    if (requestedTab === 'whats-new' && isAdmin) return <Navigate to="/admin?tab=whatsnew" replace />;

    return (
        <div className="max-w-3xl mx-auto p-8">
            <header className="mb-6" data-tour="settings-header">
                <h1 className="text-2xl font-semibold text-gray-900">Settings</h1>
                <p className="text-sm text-gray-500 mt-1">Personalize how Cubie works for you.</p>
            </header>

            {/* Each section renders its own heading, so the page does not
                repeat it. */}
            <section className="min-w-0" key={identity}>
                {activeTab === 'general' && <GeneralTab />}
                {activeTab === 'ai' && <AIPersonalizationTab />}
                {activeTab === 'email' && <EmailPreferencesTab />}
                {activeTab === 'slack' && <SlackTab />}
                {activeTab === 'scheduled-tasks' && <ScheduledTasksTab />}
            </section>
        </div>
    );
};

// ── Shared option vocab ──────────────────────────────────────────────────────

const NOTIFICATION_KINDS: { value: string; label: string }[] = [
    { value: 'agent_recommendation', label: 'AI Priorities & recommendations' },
    { value: 'deal_stage_change', label: 'Deal stage changes' },
    { value: 'ingest_failure', label: 'Document ingest failures' },
    { value: 'doc_approval_request', label: 'Document approval requests' },
    { value: 'scheduled_task_result', label: 'Scheduled task results' },
    { value: 'meeting_prep_ready', label: 'Meeting prep is ready' },
    { value: 'meeting_prep_failed', label: "Meeting prep couldn't be built" },
];

const PRIORITY_SOURCES: { value: string; label: string }[] = [
    { value: 'gmail', label: 'Email' },
    { value: 'calendar', label: 'Calendar' },
    { value: 'meet', label: 'Meetings' },
    { value: 'hubspot', label: 'HubSpot' },
    { value: 'drive', label: 'Drive' },
    { value: 'eos', label: 'EOS (Traction)' },
    { value: 'inherited', label: 'Offboard Handoff' },
    { value: 'promises', label: 'My To-Dos' },
    { value: 'slack', label: 'Slack' },
];
const ALL_SOURCE_VALUES = PRIORITY_SOURCES.map(s => s.value);

const LANGUAGES: { value: string; label: string }[] = [
    { value: 'auto', label: 'Auto — match the question' },
    { value: 'en', label: 'English' },
    { value: 'es', label: 'Spanish' },
    { value: 'fr', label: 'French' },
    { value: 'pt', label: 'Portuguese' },
    { value: 'de', label: 'German' },
];

const TIMEZONES: string[] = (() => {
    try {
        const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
        if (typeof intl.supportedValuesOf === 'function') {
            return intl.supportedValuesOf('timeZone');
        }
    } catch {
        /* fall through to the curated list */
    }
    return [
        'America/Los_Angeles', 'America/Denver', 'America/Phoenix', 'America/Chicago',
        'America/New_York', 'America/Anchorage', 'Pacific/Honolulu', 'UTC',
        'Europe/London', 'Europe/Madrid', 'America/Mexico_City',
    ];
})();
const BROWSER_TZ = (() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch { return ''; }
})();

const SaveBar: React.FC<{ saving: boolean; saved: boolean; onSave: () => void; disabled?: boolean }> = ({
    saving, saved, onSave, disabled,
}) => (
    <div className="mt-5 flex items-center gap-3">
        <button
            onClick={onSave}
            disabled={saving || disabled}
            className="flex items-center gap-2 px-4 py-2 bg-brand text-white rounded-md text-sm font-medium hover:bg-brand-hover disabled:opacity-50 transition-colors"
        >
            {saving ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
            Save
        </button>
        {saved && !saving && (
            <span className="flex items-center gap-1 text-xs text-green-700">
                <CheckCircle2 size={14} /> Saved
            </span>
        )}
    </div>
);

const ErrorBanner: React.FC<{ error: string | null }> = ({ error }) =>
    error ? (
        <div className="mb-4 flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200 text-sm text-red-800">
            <AlertCircle size={16} className="flex-shrink-0 mt-0.5" />
            <span>{error}</span>
        </div>
    ) : null;

const CheckRow: React.FC<{ checked: boolean; onChange: (v: boolean) => void; label: string }> = ({
    checked, onChange, label,
}) => (
    <label className="flex items-center gap-2 text-sm text-gray-800 cursor-pointer py-1">
        <input
            type="checkbox"
            checked={checked}
            onChange={(e) => onChange(e.target.checked)}
            className="rounded border-gray-300 text-brand focus:ring-brand"
        />
        {label}
    </label>
);

// Comma/Enter-driven chip input → string[].
const TagInput: React.FC<{ value: string[]; onChange: (v: string[]) => void; placeholder?: string }> = ({
    value, onChange, placeholder,
}) => {
    const [draft, setDraft] = useState('');
    const commit = () => {
        const t = draft.trim();
        if (t && !value.includes(t)) onChange([...value, t]);
        setDraft('');
    };
    return (
        <div className="flex flex-wrap gap-2 items-center border border-gray-300 rounded-md px-2 py-2 focus-within:border-brand focus-within:ring-1 focus-within:ring-brand">
            {value.map((tag) => (
                <span key={tag} className="inline-flex items-center gap-1 bg-brand-tint text-brand-hover text-xs rounded-full px-2 py-1">
                    {tag}
                    <button type="button" onClick={() => onChange(value.filter(t => t !== tag))} className="hover:text-brand">
                        <X size={12} />
                    </button>
                </span>
            ))}
            <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit(); }
                    else if (e.key === 'Backspace' && !draft && value.length) onChange(value.slice(0, -1));
                }}
                onBlur={commit}
                placeholder={value.length ? '' : placeholder}
                className="flex-1 min-w-[8rem] text-sm outline-none py-1"
            />
        </div>
    );
};

const GeneralTab: React.FC = () => {
    const { hasFeature } = useAuth();
    const canUseVoiceCommands = hasFeature('voice_commands');
    const canUsePageTours = hasFeature('page_tours');
    const canUseChromeExtension = hasFeature('chrome_extension');
    const [tz, setTz] = useState('');
    const [whStart, setWhStart] = useState('');
    const [whEnd, setWhEnd] = useState('');
    const [muted, setMuted] = useState<string[]>([]);
    const [voiceShortcut, setVoiceShortcut] = useState<VoiceShortcutPref | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        client.get('/api/preferences')
            .then(res => {
                setTz(res.data.timezone ?? '');
                setWhStart(res.data.working_hours_start ?? '');
                setWhEnd(res.data.working_hours_end ?? '');
                setMuted(Array.isArray(res.data.muted_notification_kinds) ? res.data.muted_notification_kinds : []);
                setVoiceShortcut(res.data.voice_shortcut ?? null);
            })
            .catch(err => setError(err?.response?.data?.detail || err.message))
            .finally(() => setLoading(false));
    }, []);

    const dirty = () => setSaved(false);

    const handleSave = async () => {
        if (whStart && whEnd && whEnd <= whStart) {
            setError('Working hours end must be after the start.');
            return;
        }
        setSaving(true);
        setError(null);
        try {
            await client.put('/api/preferences', {
                timezone: tz || null,
                working_hours_start: whStart || null,
                working_hours_end: whEnd || null,
                muted_notification_kinds: muted,
                // Sent only by someone who has the Voice button, so saving the
                // tab never touches a shortcut they cannot see.
                ...(canUseVoiceCommands ? { voice_shortcut: voiceShortcut } : {}),
            });
            if (canUseVoiceCommands) {
                // The live shortcut changes now, without a reload.
                window.dispatchEvent(new CustomEvent(VOICE_SHORTCUT_EVENT, { detail: voiceShortcut }));
            }
            setSaved(true);
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <div className="p-8 flex items-center gap-2 text-gray-500">
                <Loader2 className="animate-spin" size={16} /> Loading…
            </div>
        );
    }

    return (
        <>
            <header className="mb-4">
                <h2 className="text-lg font-semibold text-gray-900">General</h2>
                <p className="text-sm text-gray-500 mt-1">
                    Your timezone, working hours, voice command shortcut, page tours, which notifications reach you, and the browsers connected to Cubie for Chrome.
                </p>
            </header>

            <ErrorBanner error={error} />

            <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                <Field label="Timezone" hint="Cubie anchors your AI Priorities and daily briefing to your local day. Defaults to your company's business time zone if unset.">
                    <select
                        value={tz}
                        onChange={(e) => { setTz(e.target.value); dirty(); }}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    >
                        <option value="">— Use company default —</option>
                        {TIMEZONES.map(z => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
                    </select>
                    {BROWSER_TZ && tz !== BROWSER_TZ && (
                        <button
                            type="button"
                            onClick={() => { setTz(BROWSER_TZ); dirty(); }}
                            className="mt-2 text-xs text-brand hover:underline"
                        >
                            Use my current timezone ({BROWSER_TZ.replace(/_/g, ' ')})
                        </button>
                    )}
                </Field>

                <Field label="Working hours" hint="Cubie keeps focus blocks and free-slot suggestions inside this window.">
                    <div className="flex items-center gap-3">
                        <input
                            type="time"
                            value={whStart}
                            onChange={(e) => { setWhStart(e.target.value); dirty(); }}
                            className="border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        />
                        <span className="text-sm text-gray-500">to</span>
                        <input
                            type="time"
                            value={whEnd}
                            onChange={(e) => { setWhEnd(e.target.value); dirty(); }}
                            className="border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        />
                    </div>
                </Field>

                {canUseVoiceCommands && (
                    <Field
                        label="Voice command shortcut"
                        hint="Press it anywhere in Cubie to give a voice command: tap it, speak, and tap it again (or press Enter), or hold it while you talk and let go. Pick keys no other app on your computer uses."
                    >
                        <VoiceShortcutSetting
                            value={voiceShortcut}
                            onChange={(next) => { setVoiceShortcut(next); dirty(); }}
                        />
                    </Field>
                )}

                {canUsePageTours && (
                    <Field
                        label="Page tours"
                        hint="A few short cards that walk you through a page the first time you open it. Saved as soon as you change it. You can replay a page's tour any time from the account menu."
                    >
                        <div data-tour="settings-page-tours">
                            <PageToursSetting />
                        </div>
                    </Field>
                )}

                <Field label="Notifications" hint="Hide notification types you don't want in the bell. (Slack delivery is managed by your admin per department.)">
                    <div className="space-y-0.5">
                        {NOTIFICATION_KINDS.map(k => (
                            <CheckRow
                                key={k.value}
                                label={`Mute ${k.label}`}
                                checked={muted.includes(k.value)}
                                onChange={(on) => {
                                    setMuted(prev => on ? [...prev, k.value] : prev.filter(v => v !== k.value));
                                    dirty();
                                }}
                            />
                        ))}
                    </div>
                </Field>
            </div>

            <SaveBar saving={saving} saved={saved} onSave={handleSave} />

            {/* Its own card, below Save: connecting and disconnecting take
                effect at once and are not part of the form above. */}
            {canUseChromeExtension && <CubieForChromeCard />}
        </>
    );
};

const AIPersonalizationTab: React.FC = () => {
    const [roleTitle, setRoleTitle] = useState('');
    const [aboutMe, setAboutMe] = useState('');
    const [verbosity, setVerbosity] = useState('');
    const [language, setLanguage] = useState('');
    const [pFocus, setPFocus] = useState('');
    const [pMax, setPMax] = useState<string>('');
    const [pSources, setPSources] = useState<string[]>(ALL_SOURCE_VALUES);
    const [pMuted, setPMuted] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        client.get('/api/preferences')
            .then(res => {
                setRoleTitle(res.data.role_title ?? '');
                setAboutMe(res.data.about_me ?? '');
                setVerbosity(res.data.chat_verbosity ?? '');
                setLanguage(res.data.response_language ?? '');
                setPFocus(res.data.priorities_focus ?? '');
                setPMax(res.data.priorities_max != null ? String(res.data.priorities_max) : '');
                const src = res.data.priorities_sources;
                // A saved source this list no longer offers (a removed channel)
                // is dropped; the rest of the choice is kept.
                const known = Array.isArray(src) ? src.filter((v: unknown) => typeof v === 'string' && ALL_SOURCE_VALUES.includes(v)) : [];
                setPSources(known.length ? known : ALL_SOURCE_VALUES);
                setPMuted(Array.isArray(res.data.priorities_muted_topics) ? res.data.priorities_muted_topics : []);
            })
            .catch(err => setError(err?.response?.data?.detail || err.message))
            .finally(() => setLoading(false));
    }, []);

    const dirty = () => setSaved(false);

    const handleSave = async () => {
        if (pSources.length === 0) {
            setError('Pick at least one AI Priorities source.');
            return;
        }
        setSaving(true);
        setError(null);
        try {
            await client.put('/api/preferences', {
                role_title: roleTitle || null,
                about_me: aboutMe || null,
                chat_verbosity: verbosity || null,
                response_language: language || null,
                priorities_focus: pFocus || null,
                priorities_max: pMax ? Number(pMax) : null,
                // All sources selected → null (means "use the default set").
                priorities_sources: pSources.length === ALL_SOURCE_VALUES.length ? null : pSources,
                priorities_muted_topics: pMuted,
            });
            setSaved(true);
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <div className="p-8 flex items-center gap-2 text-gray-500">
                <Loader2 className="animate-spin" size={16} /> Loading…
            </div>
        );
    }

    return (
        <>
            <header className="mb-4">
                <h2 className="text-lg font-semibold text-gray-900">AI Personalization</h2>
                <p className="text-sm text-gray-500 mt-1">
                    Tell Cubie who you are and how you like to work. This shapes your Chat answers,
                    AI Priorities, and meeting prep.
                </p>
            </header>

            <ErrorBanner error={error} />

            <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                <Field label="Your role" hint="Your title or role — helps Cubie frame answers for what you do.">
                    <input
                        type="text"
                        value={roleTitle}
                        onChange={(e) => { setRoleTitle(e.target.value); dirty(); }}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="Account Executive"
                    />
                </Field>

                <Field label="About you & your focus" hint="What you're responsible for and care about most right now. Cubie weights priorities and tailors answers toward this.">
                    <textarea
                        value={aboutMe}
                        onChange={(e) => { setAboutMe(e.target.value); dirty(); }}
                        rows={3}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="I own new-business deals in the West region. Prioritize my open pipeline and renewals; deprioritize internal ops."
                    />
                </Field>

                <Field label="Answer length" hint="How detailed Cubie's Chat answers should be.">
                    <select
                        value={verbosity}
                        onChange={(e) => { setVerbosity(e.target.value); dirty(); }}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    >
                        <option value="">— Default —</option>
                        <option value="concise">Concise</option>
                        <option value="balanced">Balanced</option>
                        <option value="detailed">Detailed</option>
                    </select>
                </Field>

                <Field label="Reply language" hint="The language Cubie answers in, whatever language you ask in.">
                    <select
                        value={language}
                        onChange={(e) => { setLanguage(e.target.value); dirty(); }}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    >
                        {LANGUAGES.map(l => <option key={l.value} value={l.value}>{l.label}</option>)}
                    </select>
                </Field>
            </div>

            <h3 className="text-sm font-semibold text-gray-900 mt-8 mb-2">AI Priorities</h3>
            <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                <Field label="Default focus" hint="A standing instruction for what your priorities should center on. The one-off focus box on the home page still overrides this per run.">
                    <input
                        type="text"
                        value={pFocus}
                        onChange={(e) => { setPFocus(e.target.value); dirty(); }}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="My open deals and anything blocking a close this week"
                    />
                </Field>

                <Field label="How many priorities" hint="How many cards Cubie shows by default (1–10). Leave blank for the standard 5.">
                    <input
                        type="number"
                        min={1}
                        max={10}
                        value={pMax}
                        onChange={(e) => { setPMax(e.target.value); dirty(); }}
                        className="w-28 border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="5"
                    />
                </Field>

                <Field label="Sources" hint="Which signals Cubie may draw priorities from.">
                    <div className="space-y-0.5">
                        {PRIORITY_SOURCES.map(s => (
                            <CheckRow
                                key={s.value}
                                label={s.label}
                                checked={pSources.includes(s.value)}
                                onChange={(on) => {
                                    setPSources(prev => on ? [...prev, s.value] : prev.filter(v => v !== s.value));
                                    dirty();
                                }}
                            />
                        ))}
                    </div>
                </Field>

                <Field label="Muted topics" hint="Cubie will never surface a priority about these. Type a topic and press Enter.">
                    <TagInput value={pMuted} onChange={(v) => { setPMuted(v); dirty(); }} placeholder="e.g. office logistics" />
                </Field>
            </div>

            <SaveBar saving={saving} saved={saved} onSave={handleSave} />

            <DismissedSnoozedList />
        </>
    );
};

interface HistoryItem {
    entity_kind: string;
    entity_id: string;
    priority_kind: string;
    state: string;
    snoozed_until: string | null;
    label: string;
}

const DismissedSnoozedList: React.FC = () => {
    const [items, setItems] = useState<HistoryItem[] | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    const load = () => {
        client.get<HistoryItem[]>('/api/homefeed/priorities/history')
            .then(res => setItems(res.data))
            .catch(err => setError(err?.response?.data?.detail || err.message));
    };
    useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

    const reopen = async (item: HistoryItem) => {
        const id = `${item.entity_kind}:${item.entity_id}:${item.priority_kind}`;
        setBusy(id);
        setError(null);
        try {
            await client.post('/api/homefeed/priorities/history/reopen', {
                entity_kind: item.entity_kind,
                entity_id: item.entity_id,
                priority_kind: item.priority_kind,
            });
            setItems(prev => (prev ? prev.filter(i =>
                !(i.entity_kind === item.entity_kind && i.entity_id === item.entity_id && i.priority_kind === item.priority_kind)
            ) : prev));
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setBusy(null);
        }
    };

    return (
        <section className="mt-8">
            <h3 className="text-sm font-semibold text-gray-900 mb-1">Handled priorities</h3>
            <p className="text-xs text-gray-500 mb-3">
                Reopen something you dismissed, snoozed or marked done so Cubie can
                surface it again. Items marked done stay hidden permanently until you
                reopen them here.
            </p>
            <ErrorBanner error={error} />
            {items === null ? (
                <div className="flex items-center gap-2 text-gray-500 text-sm py-2">
                    <Loader2 className="animate-spin" size={14} /> Loading…
                </div>
            ) : items.length === 0 ? (
                <div className="bg-white rounded-lg border border-gray-200 p-5 text-sm text-gray-500">
                    Nothing dismissed, snoozed or marked done right now.
                </div>
            ) : (
                <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                    {items.map(item => {
                        const id = `${item.entity_kind}:${item.entity_id}:${item.priority_kind}`;
                        return (
                            <div key={id} className="flex items-center justify-between gap-3 p-4">
                                <div className="min-w-0">
                                    <p className="text-sm text-gray-900 truncate">{item.label}</p>
                                    <p className="text-xs text-gray-500 mt-0.5">
                                        <span className={`inline-block rounded-full px-2 py-0.5 ${item.state === 'snoozed' ? 'bg-amber-50 text-amber-700' : item.state === 'resolved' ? 'bg-emerald-50 text-emerald-700' : 'bg-gray-100 text-gray-600'}`}>
                                            {item.state === 'snoozed' ? 'Snoozed' : item.state === 'resolved' ? 'Done' : 'Dismissed'}
                                        </span>
                                        {item.state === 'resolved' && (
                                            <span className="ml-2">won&apos;t come back on its own</span>
                                        )}
                                        {item.state === 'snoozed' && item.snoozed_until && (
                                            <span className="ml-2">until {formatInstantDate(item.snoozed_until)}</span>
                                        )}
                                    </p>
                                </div>
                                <button
                                    onClick={() => reopen(item)}
                                    disabled={busy === id}
                                    className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-300 text-gray-700 rounded-md text-xs font-medium hover:bg-gray-50 disabled:opacity-50 transition-colors flex-shrink-0"
                                >
                                    {busy === id ? <Loader2 className="animate-spin" size={12} /> : <RotateCcw size={12} />}
                                    Reopen
                                </button>
                            </div>
                        );
                    })}
                </div>
            )}
        </section>
    );
};

const EmailPreferencesTab: React.FC = () => {
    const [prefs, setPrefs] = useState<Preferences>(EMPTY);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [savedAt, setSavedAt] = useState<string | null>(null);
    const [previewing, setPreviewing] = useState(false);
    const [preview, setPreview] = useState<PreviewResult | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        client.get('/api/preferences')
            .then(res => {
                setPrefs({
                    writing_style: res.data.writing_style ?? '',
                    tone: (res.data.tone ?? '') as Tone | '',
                    preferred_greeting: res.data.preferred_greeting ?? '',
                    additional_instructions: res.data.additional_instructions ?? '',
                });
            })
            .catch(err => setError(err?.response?.data?.detail || err.message))
            .finally(() => setLoading(false));
    }, []);

    const handleChange = <K extends keyof Preferences>(key: K, value: Preferences[K]) => {
        setPrefs(p => ({ ...p, [key]: value }));
        setSavedAt(null);
    };

    const handleSave = async () => {
        setSaving(true);
        setError(null);
        try {
            const payload = {
                writing_style: prefs.writing_style || null,
                tone: prefs.tone || null,
                preferred_greeting: prefs.preferred_greeting || null,
                additional_instructions: prefs.additional_instructions || null,
            };
            const res = await client.put('/api/preferences', payload);
            setSavedAt(res.data.updated_at || new Date().toISOString());
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setSaving(false);
        }
    };

    const handlePreview = async () => {
        setPreviewing(true);
        setError(null);
        setPreview(null);
        try {
            const res = await client.post('/api/preferences/preview');
            setPreview(res.data);
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setPreviewing(false);
        }
    };

    if (loading) {
        return (
            <div className="p-8 flex items-center gap-2 text-gray-500">
                <Loader2 className="animate-spin" size={16} /> Loading…
            </div>
        );
    }

    return (
        <>
            <header className="mb-4">
                <h2 className="text-lg font-semibold text-gray-900">Email Preferences</h2>
                <p className="text-sm text-gray-500 mt-1">
                    Set the voice every AI-generated email and meeting brief should use on your behalf.
                    Your Google Workspace email signature is appended automatically — no need to set it here.
                </p>
            </header>

            <GmailAccountsCard />

            {error && (
                <div className="mb-4 flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200 text-sm text-red-800">
                    <AlertCircle size={16} className="flex-shrink-0 mt-0.5" />
                    <span>{error}</span>
                </div>
            )}

            <div className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                <Field label="Tone" hint="The overall feel of your communication.">
                    <select
                        value={prefs.tone}
                        onChange={(e) => handleChange('tone', e.target.value as Tone | '')}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    >
                        <option value="">— Not set —</option>
                        <option value="formal">Formal</option>
                        <option value="conversational">Conversational</option>
                        <option value="direct">Direct</option>
                    </select>
                </Field>

                <Field label="Writing style" hint="Free-form description of how you write — sentence length, vocabulary, structure.">
                    <textarea
                        value={prefs.writing_style}
                        onChange={(e) => handleChange('writing_style', e.target.value)}
                        rows={3}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="Short sentences. No corporate jargon. Lead with the recipient's problem."
                    />
                </Field>

                <Field label="Preferred greeting" hint="How emails should open.">
                    <input
                        type="text"
                        value={prefs.preferred_greeting}
                        onChange={(e) => handleChange('preferred_greeting', e.target.value)}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="Hey {first_name},"
                    />
                </Field>

                <Field label="Additional instructions" hint="Anything else the agent should always do (or never do).">
                    <textarea
                        value={prefs.additional_instructions}
                        onChange={(e) => handleChange('additional_instructions', e.target.value)}
                        rows={3}
                        className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                        placeholder="Always close with a single concrete next step. Never use the phrase 'circle back'."
                    />
                </Field>
            </div>

            <div className="mt-5 flex items-center gap-3">
                <button
                    onClick={handleSave}
                    disabled={saving}
                    className="flex items-center gap-2 px-4 py-2 bg-brand text-white rounded-md text-sm font-medium hover:bg-brand-hover disabled:opacity-50 transition-colors"
                >
                    {saving ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                    Save preferences
                </button>
                <button
                    onClick={handlePreview}
                    disabled={previewing}
                    className="flex items-center gap-2 px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 disabled:opacity-50 transition-colors"
                >
                    {previewing ? <Loader2 className="animate-spin" size={14} /> : <Sparkles size={14} />}
                    Preview my style
                </button>
                {savedAt && !saving && (
                    <span className="flex items-center gap-1 text-xs text-green-700">
                        <CheckCircle2 size={14} /> Saved
                    </span>
                )}
            </div>

            {preview && (
                <section className="mt-8 bg-gray-50 border border-gray-200 rounded-lg p-5">
                    <h3 className="text-sm font-semibold text-gray-900 mb-3">Sample email using your preferences</h3>
                    {preview.subject && (
                        <p className="text-sm mb-2">
                            <span className="font-semibold text-gray-700">Subject:</span>{' '}
                            <span className="text-gray-900">{preview.subject}</span>
                        </p>
                    )}
                    <pre className="whitespace-pre-wrap text-sm text-gray-800 font-sans">
                        {preview.body || preview.raw_response}
                    </pre>
                    <p className="text-xs text-gray-500 mt-3">
                        Sample only — not saved to Gmail. Generated against a fictional prospect.
                        Your Google Workspace signature would be appended on a real send.
                    </p>
                </section>
            )}
        </>
    );
};

const Field: React.FC<{ label: string; hint: string; children: React.ReactNode }> = ({ label, hint, children }) => (
    <div className="p-5">
        <label className="block text-sm font-medium text-gray-900 mb-1">{label}</label>
        <p className="text-xs text-gray-500 mb-2">{hint}</p>
        {children}
    </div>
);

interface SlackStatus {
    is_connected: boolean;
    is_configured: boolean;
    team_name?: string | null;
    slack_user_id?: string | null;
    connected_at?: string | null;
}

const SlackTab: React.FC = () => {
    const [searchParams, setSearchParams] = useSearchParams();
    const [status, setStatus] = useState<SlackStatus | null>(null);
    const [loading, setLoading] = useState(true);
    const [working, setWorking] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [flashMessage, setFlashMessage] = useState<string | null>(null);

    // Surface OAuth callback outcomes (success or error) the router redirected
    // back with, then strip them from the URL so a refresh doesn't re-trigger.
    useEffect(() => {
        const connected = searchParams.get('slack_connected');
        const slackError = searchParams.get('slack_error');
        if (connected) {
            setFlashMessage('Slack connected successfully.');
            const next = new URLSearchParams(searchParams);
            next.delete('slack_connected');
            setSearchParams(next, { replace: true });
        } else if (slackError) {
            setError(`Slack connect failed: ${slackError}`);
            const next = new URLSearchParams(searchParams);
            next.delete('slack_error');
            setSearchParams(next, { replace: true });
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const refresh = async () => {
        setLoading(true);
        try {
            const res = await client.get('/api/slack/status');
            setStatus(res.data);
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        refresh();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const handleConnect = async () => {
        setWorking(true);
        setError(null);
        try {
            const res = await client.get('/api/slack/oauth-start');
            window.location.href = res.data.authorize_url;
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
            setWorking(false);
        }
    };

    const handleDisconnect = async () => {
        if (!window.confirm('Disconnect Slack? You can reconnect anytime.')) return;
        setWorking(true);
        setError(null);
        try {
            await client.post('/api/slack/disconnect');
            setFlashMessage('Slack disconnected.');
            await refresh();
        } catch (err: any) {
            setError(err?.response?.data?.detail || err.message);
        } finally {
            setWorking(false);
        }
    };

    if (loading) {
        return (
            <div className="p-8 flex items-center gap-2 text-gray-500">
                <Loader2 className="animate-spin" size={16} /> Loading…
            </div>
        );
    }

    return (
        <>
            <header data-tour="settings-slack" className="mb-4">
                <h2 className="text-lg font-semibold text-gray-900">Slack</h2>
                <p className="text-sm text-gray-500 mt-1">
                    Connect Slack so Cubie can surface @-mentions in AI Priorities and answer
                    questions like "what's happening in #sales" or "summarize my Slack mentions".
                    Connect here before using Cubie's /todo or /ids commands in Slack.
                </p>
            </header>

            {error && (
                <div className="mb-4 flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200 text-sm text-red-800">
                    <AlertCircle size={16} className="flex-shrink-0 mt-0.5" />
                    <span>{error}</span>
                </div>
            )}

            {flashMessage && (
                <div className="mb-4 flex items-start gap-2 p-3 rounded-md bg-green-50 border border-green-200 text-sm text-green-800">
                    <CheckCircle2 size={16} className="flex-shrink-0 mt-0.5" />
                    <span>{flashMessage}</span>
                </div>
            )}

            {status && !status.is_configured && (
                <div className="mb-4 flex items-start gap-2 p-3 rounded-md bg-yellow-50 border border-yellow-200 text-sm text-yellow-800">
                    <AlertCircle size={16} className="flex-shrink-0 mt-0.5" />
                    <span>
                        Slack OAuth isn't configured on this deployment yet. Ask an admin to set
                        <code className="mx-1 px-1 rounded bg-yellow-100">SLACK_CLIENT_ID</code>,
                        <code className="mx-1 px-1 rounded bg-yellow-100">SLACK_CLIENT_SECRET</code>, and
                        <code className="mx-1 px-1 rounded bg-yellow-100">SLACK_REDIRECT_URI</code>.
                    </span>
                </div>
            )}

            <div className="bg-white rounded-lg border border-gray-200 p-6">
                {status?.is_connected ? (
                    <div className="flex items-center justify-between gap-4">
                        <div>
                            <div className="flex items-center gap-2 text-sm font-medium text-gray-900">
                                <CheckCircle2 size={16} className="text-green-600" />
                                Connected to <span>{status.team_name || 'Slack'}</span>
                            </div>
                            {status.connected_at && (
                                <p className="text-xs text-gray-500 mt-1">
                                    Authorized {formatInstant(status.connected_at)}
                                </p>
                            )}
                        </div>
                        <button
                            onClick={handleDisconnect}
                            disabled={working}
                            className="flex items-center gap-2 px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 disabled:opacity-50 transition-colors"
                        >
                            {working ? <Loader2 className="animate-spin" size={14} /> : <Unlink size={14} />}
                            Disconnect
                        </button>
                    </div>
                ) : (
                    <div className="flex items-center justify-between gap-4">
                        <div>
                            <p className="text-sm font-medium text-gray-900">Slack is not connected.</p>
                            <p className="text-xs text-gray-500 mt-1">
                                You'll be redirected to Slack to authorize. We only request read scopes
                                plus the ability to post on your behalf in workspaces you install us in.
                            </p>
                        </div>
                        <button
                            onClick={handleConnect}
                            disabled={working || !status?.is_configured}
                            className="flex items-center gap-2 px-4 py-2 bg-brand text-white rounded-md text-sm font-medium hover:bg-brand-hover disabled:opacity-50 transition-colors"
                        >
                            {working ? <Loader2 className="animate-spin" size={14} /> : <Link2 size={14} />}
                            Connect Slack
                        </button>
                    </div>
                )}
            </div>
        </>
    );
};
