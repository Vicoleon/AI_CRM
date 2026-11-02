// UC-39 — AI Priorities panel with smart action buttons.
//
// Each card has the three primitives ✓/⏰/✗ visible by default, plus a
// collapsible "More actions" section (closed by default) that surfaces:
//   - Snooze options: Tomorrow / In 3 days / Next week / Custom
//   - Schedule 30 min focus  → POST /api/calendar/focus-block
//   - Entity-specific verb driven by priority.entity_kind:
//       deal → Open in HubSpot (deep-link)
//       thread → Reply (Gmail draft via UC-27 → resolves FOLLOW_UP_DEBT)
//       event → Generate prep (UC-26 → resolves PREP_DEBT)
//       transcript → Generate follow-up email (UC-39 → resolves COMMITMENT_DEBT)
//       person / company / document → deep-links
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { formatInstantTime } from '../utils/datetime';
import {
    Sparkles, RefreshCw, ExternalLink, Check, Clock, X, AlertCircle,
    CalendarPlus, Reply, Briefcase, FileText,
    Building2, User as UserIcon, ListChecks,
    MessageSquare, Target, Flag, CalendarX, CalendarClock,
} from 'lucide-react';
import client from '../api/client';
import { useAuth } from '../context/AuthContext';
import { DeferDealModal } from './sales/DeferDealModal';
import type { DeferTarget } from '../api/dealDefer';
import { FeedbackButtons } from './FeedbackButtons';
import type { FeedbackRating } from './FeedbackButtons';
import {
    priorityComposeUrl,
    priorityThreadUrl,
} from './priorityGmail';


// ─── Types ───────────────────────────────────────────────────────────────────

type EntityKind =
    | 'deal' | 'thread' | 'event' | 'transcript'
    | 'person' | 'company' | 'document'
    // EOS entities — surfaced on the AI-Priorities panel with in-app actions.
    | 'rock' | 'issue' | 'todo' | 'milestone'
    // One of the reader's own My To-Dos (entity_id = promises.id). ✓ on the
    // card marks the promise kept — see backend/routers/home.py.
    | 'promise';

// A promise Cubie heard the reader make, past its moment with no sign it was
// kept (rubik_core/promises/accountability.py).
const KIND_PROMISE_UNKEPT = 'promise_unkept';

// The facts a "You said you would…" card is honest only with. Projected from
// the server's card-attr allow-list (rubik_core/priorities/models.py).
interface PromiseCardAttrs {
    promise_quote?: string | null;
    promise_said_on?: string | null;
    promise_where?: string | null;
    promise_checked?: string | null;
    promise_link?: string | null;
    gmail_mailbox?: string | null;
    gmail_account_id?: string | null;
    personal_gmail?: boolean | null;
    private_gmail_context?: boolean | null;
}

interface PriorityItem {
    id: string;
    title: string;
    why_it_matters: string;
    urgency: 'high' | 'medium' | 'low' | string;
    source_channels: string[];
    source_links: string[];
    suggested_action: string;
    priority_kind?: string | null;
    entity_kind?: EntityKind | null;
    entity_id?: string | null;
    entity_label?: string | null;
    // Set only for a RECURRING calendar event. Each occurrence has its own
    // entity_id, so a plain dismiss can't reach next week's — its presence is
    // what shows the "never prep this again" button.
    series_entity_id?: string | null;
    // "priority" (default) or "recommendation". A recommendation comes from
    // /priorities/more and routes its actions to the /agents endpoints.
    // "next_week" is a Friday look-ahead item; it uses the normal priority
    // actions (same as "priority") but wears the "Next week" badge.
    origin?: 'priority' | 'recommendation' | 'next_week';
    // For origin="next_week": ISO date (Monday) the target week begins.
    week_of?: string | null;
    // Set ONLY on a manager's card for a direct report's EOS item: the report
    // to nudge (target_email) and their display name (target_label). Drives the
    // "Slack follow-up" action instead of a self-scheduling one.
    target_email?: string | null;
    target_label?: string | null;
    // EVERY entity this card speaks for, primary first. A card that aggregates
    // siblings ("two overdue To-Dos") lists them all here; entity_id is merely
    // whichever one was cited first.
    covered_entity_ids?: string[] | null;
    covered_entity_labels?: string[] | null;
    // Ownership PER covered entity, positional against covered_entity_ids. One
    // card can mix your item with a colleague's — siblings are grouped by
    // situation, not by owner. `null` in a slot means the card never carried
    // the flag ("unknown", never "not yours").
    covered_entity_is_own?: (boolean | null)[] | null;
    // Does the reader OWN this item (the PRIMARY entity)? Absent on older
    // cards, which are all the reader's own by construction, so `!== false`
    // is the test.
    is_own?: boolean | null;
    // The card's own facts (today only a My To-Dos card carries any). `attrs`
    // is null on a card that carries none, so the card renders the honest
    // subset it has and infers nothing that is missing.
    attrs?: PromiseCardAttrs | null;
}

interface PrioritiesResponse {
    generated_at: string;
    headline: string;
    priorities: PriorityItem[];
    revision_count: number;
    last_critique_score: number | null;
    // How many priorities today's card had BEFORE the read-path memory filter.
    // generated_count > 0 with none remaining => the user cleared their list.
    generated_count?: number;
    // Out-of-office banner payload when the user is on PTO today, else absent.
    pto?: {
        on_pto: boolean;
        start: string; // YYYY-MM-DD
        end: string; // YYYY-MM-DD
        return_date: string; // YYYY-MM-DD
    } | null;
    // Upcoming invites you never answered, else absent. These deliberately do
    // NOT become priorities — an unanswered invite isn't a commitment — so this
    // note is how you still hear about them. `invites` is capped and can be
    // shorter than `unanswered_count`.
    calendar_hygiene?: {
        unanswered_count: number;
        invites: { title: string; when_local?: string | null; link?: string | null }[];
    } | null;
}

interface MoreResponse {
    status: 'recommendations' | 'running' | 'already_running';
    items: PriorityItem[];
    run_id?: string | null;
}

// State for the preview-then-send Slack nudge modal (a manager nudging a
// report about their about-to-be-due EOS item).
interface SlackFollowupState {
    priorityId: string;
    targetLabel: string;
    message: string;
    loading: boolean; // fetching the preview text
    sending: boolean; // delivering the DM
    notice: string | null; // friendly, in-modal reason a send didn't go out
}

// Shown when the user has cleared every priority for the day. One is picked at
// random per card load.
const DONE_PHRASES = [
    "You're done for the day — nice work. 🎉",
    'All clear. Everything on your plate is handled.',
    'Priorities: zero left. Go take a breather. 👏',
    'Crushed it — nothing else needs you right now.',
    "That's a wrap. Every priority handled today.",
];


// ─── Entity-verb mapping ─────────────────────────────────────────────────────
//
// One helper, one place to extend. When a new EntityKind lands in UC-22,
// add an entry here and the card renders the right verb automatically.

interface EntityAction {
    label: string;
    icon: React.ReactNode;
    onClick: (p: PriorityItem, ctx: ActionContext) => void;
}

interface ActionContext {
    navigate: ReturnType<typeof useNavigate>;
    triggerFocusBlock: (p: PriorityItem) => void;
    setError: (msg: string | null) => void;
    refresh: (force: boolean) => void;
    // Opens the preview-then-send Slack nudge modal for a report's EOS item.
    openSlackFollowup: (p: PriorityItem) => void;
}

function entityAction(p: PriorityItem): EntityAction | null {
    switch (p.entity_kind) {
        case 'deal':
            return {
                label: 'Open in HubSpot',
                icon: <Building2 size={13} />,
                onClick: () => {
                    // HubSpot deal deep-link. The portal id varies per org; we
                    // try the source_links first (the agent may already have
                    // captured an absolute URL), then fall back to a generic
                    // path that HubSpot's web app routes correctly.
                    const url = p.source_links?.[0]
                        || `https://app.hubspot.com/contacts/0/deal/${p.entity_id}`;
                    window.open(url, '_blank', 'noopener,noreferrer');
                },
            };
        case 'thread': {
            if (!p.source_channels?.includes('gmail')) return null;
            const url = priorityThreadUrl(p.attrs, p.entity_id);
            if (!url) {
                // A card citing several mailboxes cannot choose one for a
                // reply, so keep the action unavailable.
                return null;
            }
            return {
                label: 'Reply',
                icon: <Reply size={13} />,
                onClick: () => {
                    // Open the exact mailbox cited by this thread. A personal
                    // thread's entity ID is namespaced by account ID; Gmail
                    // itself needs the raw thread ID and authuser mailbox.
                    window.open(url, '_blank', 'noopener,noreferrer');
                },
            };
        }
        case 'event':
            return {
                label: 'Generate prep',
                icon: <ListChecks size={13} />,
                onClick: (priority, ctx) => {
                    if (!priority.entity_id) return;
                    // UC-26 auto-resolves PREP_DEBT after a successful prep run.
                    ctx.navigate(`/meeting-prep/${encodeURIComponent(priority.entity_id)}`);
                },
            };
        case 'promise': {
            // A heard promise opens where it was said (the meeting, the email,
            // the Slack message) — most people this card reaches have no My
            // To-Dos page. The link comes from the ledger, never the model.
            const link = p.priority_kind === KIND_PROMISE_UNKEPT ? p.attrs?.promise_link : null;
            if (link) {
                return {
                    label: 'See where you said it',
                    icon: <FileText size={13} />,
                    onClick: (_priority, ctx) => {
                        if (link.startsWith('/')) ctx.navigate(link);
                        else window.open(link, '_blank', 'noopener,noreferrer');
                    },
                };
            }
            if (p.priority_kind === KIND_PROMISE_UNKEPT) return null;
            return {
                label: 'Open My To-Dos',
                icon: <ListChecks size={13} />,
                onClick: (_priority, ctx) => ctx.navigate('/todos'),
            };
        }
        case 'transcript':
            return {
                label: 'Draft follow-up',
                icon: <FileText size={13} />,
                onClick: async (priority, ctx) => {
                    if (!priority.entity_id) return;
                    try {
                        const res = await client.post(
                            '/api/email/drafts/from-transcript',
                            { transcript_id: priority.entity_id },
                        );
                        const url = res.data?.gmail_draft_url;
                        if (url) {
                            window.open(url, '_blank', 'noopener,noreferrer');
                        }
                        ctx.refresh(false);
                    } catch (e: any) {
                        ctx.setError(
                            e?.response?.data?.detail || e.message || 'Draft failed',
                        );
                    }
                },
            };
        case 'person': {
            const url = priorityComposeUrl(p.attrs, p.entity_id);
            if (!url) return null;
            return {
                label: p.entity_label ? `Compose to ${p.entity_label}` : 'Compose',
                icon: <UserIcon size={13} />,
                onClick: () => window.open(url, '_blank', 'noopener,noreferrer'),
            };
        }
        case 'company':
            return {
                label: 'Open company',
                icon: <Building2 size={13} />,
                onClick: () => {
                    const url = p.source_links?.[0]
                        || `https://www.${p.entity_id}`;
                    window.open(url, '_blank', 'noopener,noreferrer');
                },
            };
        case 'document':
            return {
                label: 'Open in Drive',
                icon: <FileText size={13} />,
                onClick: () => {
                    const url = p.source_links?.[0]
                        || `https://drive.google.com/file/d/${p.entity_id}/view`;
                    window.open(url, '_blank', 'noopener,noreferrer');
                },
            };
        // ── EOS — every priority is actionable from Cubie ──────────────────
        // A report's item (target_email set on the manager's card) offers a
        // one-tap Slack nudge; the user's OWN item lets them block time to do it.
        case 'todo':
        case 'milestone':
            if (p.target_email) {
                return {
                    label: 'Slack follow-up',
                    icon: <MessageSquare size={13} />,
                    onClick: (priority, ctx) => ctx.openSlackFollowup(priority),
                };
            }
            return {
                label: 'Schedule time',
                icon: <CalendarPlus size={13} />,
                onClick: (priority, ctx) => ctx.triggerFocusBlock(priority),
            };
        case 'rock':
            return {
                label: 'Update Rock',
                icon: <Target size={13} />,
                onClick: (_priority, ctx) => ctx.navigate('/eos/rocks'),
            };
        case 'issue':
            return {
                label: 'Open in IDS',
                icon: <Flag size={13} />,
                onClick: (_priority, ctx) => ctx.navigate('/eos/issues'),
            };
        default:
            return null;
    }
}


// ─── Component ───────────────────────────────────────────────────────────────

export const AIPrioritiesPanel: React.FC<{
    // Called each time a priorities load settles: cards shown, a background run
    // started, or an error. Home uses it to start getting Productivity ready
    // only once the screen people opened Cubie for has its answer.
    onSettled?: () => void;
}> = ({ onSettled }) => {
    const navigate = useNavigate();
    // Through a ref so a new callback from the parent never re-creates
    // fetchPriorities, whose identity drives the load-on-mount effect.
    const onSettledRef = useRef(onSettled);
    useEffect(() => { onSettledRef.current = onSettled; }, [onSettled]);
    const [bundle, setBundle] = useState<PrioritiesResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    // A soft "couldn't refresh" notice that does NOT replace the cards,
    // so we never show stale cards stacked under a hard red error banner.
    const [staleNotice, setStaleNotice] = useState<string | null>(null);
    // UC-41: true while polling a background pipeline run (cold load / refresh).
    const [indexing, setIndexing] = useState(false);
    const [pendingAction, setPendingAction] = useState<string | null>(null);
    const [expanded, setExpanded] = useState<Record<string, boolean>>({});
    // "Get me other important things" state.
    const [moreLoading, setMoreLoading] = useState(false);
    const [generating, setGenerating] = useState(false);
    // Set when a background "get more" run finished but surfaced nothing new.
    const [noNewItems, setNoNewItems] = useState(false);
    // WGLL feedback — the caller's existing ratings keyed by priority id, so a
    // reload restores the selected face. Best-effort; never blocks the panel.
    const [ratings, setRatings] = useState<Record<string, { rating: string; comment?: string }>>({});
    // EOS Slack follow-up: the open preview/send modal (null when closed) and a
    // transient green banner shown briefly after a nudge is delivered.
    const [slackModal, setSlackModal] = useState<SlackFollowupState | null>(null);
    const [successFlash, setSuccessFlash] = useState<string | null>(null);
    // My To-Dos gate — the "Add to my To-Dos" action is hidden for anyone the
    // server says is not on the `todos` roster (the endpoint re-checks).
    const { canAccessTodos, hasFeature } = useAuth();
    // Defer a deal: set the deal aside until a follow-up date, with a note and
    // a HubSpot task (`deal_defer` roster; the endpoint re-checks). Wider than
    // snoozing this one card — every card about the deal, and the deal's rows
    // on My Day, stay away until the date.
    const canDefer = hasFeature('deal_defer');
    const [deferTarget, setDeferTarget] = useState<DeferTarget | null>(null);

    // Mirror of `bundle` for callbacks that would otherwise close over stale state.
    const bundleRef = useRef<PrioritiesResponse | null>(null);
    useEffect(() => { bundleRef.current = bundle; }, [bundle]);

    // Single poll handle shared by the UC-41 run poller and the /more card poller.
    const pollRef = useRef<number | null>(null);
    const stopPolling = useCallback(() => {
        if (pollRef.current !== null) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
        }
    }, []);
    useEffect(() => stopPolling, [stopPolling]); // clear on unmount

    // Pick a kudos phrase once per card (stable while the user clears the last
    // few items; re-rolls when a different card loads).
    const kudosPhrase = useMemo(
        () => DONE_PHRASES[Math.floor(Math.random() * DONE_PHRASES.length)],
        [bundle?.generated_at],
    );

    // UC-41: poll a background priorities run until it's ready/failed. Shown as
    // an "indexing" skeleton instead of letting the homefeed eat a 90s timeout.
    const startRunPolling = useCallback((runId: string) => {
        stopPolling();
        setIndexing(true);
        setError(null);
        let polls = 0;
        const MAX_POLLS = 40; // ~3.5 min at 5s
        const tick = async () => {
            polls += 1;
            try {
                const res = await client.get<any>(`/api/homefeed/priorities/runs/${runId}`);
                const status = res.data?.status;
                if (status === 'ready' && res.data.bundle) {
                    stopPolling();
                    setIndexing(false);
                    setBundle(res.data.bundle as PrioritiesResponse);
                    return;
                }
                if (status === 'failed') {
                    stopPolling();
                    setIndexing(false);
                    if (bundleRef.current) {
                        setStaleNotice("Couldn't refresh just now — showing your last results.");
                    } else {
                        setError(res.data?.error || 'Could not generate your priorities. Try Refresh.');
                    }
                    return;
                }
            } catch {
                /* transient — keep polling */
            }
            if (polls >= MAX_POLLS) {
                stopPolling();
                setIndexing(false);
                if (!bundleRef.current) {
                    setError('Still indexing — give it a moment and hit Refresh.');
                }
            }
        };
        tick(); // immediate first check, then every 5s
        pollRef.current = window.setInterval(tick, 5_000);
    }, [stopPolling]);

    const fetchPriorities = useCallback(async (force = false) => {
        setLoading(true);
        setError(null);
        setStaleNotice(null);
        try {
            const res = await client.post<any>(
                '/api/homefeed/priorities',
                { force_refresh: force },
            );
            if (res.status === 202 && res.data?.run_id) {
                // UC-41: cold cache / force_refresh — the pipeline runs async.
                setLoading(false);
                startRunPolling(res.data.run_id);
                return;
            }
            setBundle(res.data as PrioritiesResponse);
        } catch (e: any) {
            // Keep stale cards + a soft notice rather than stacking a red error.
            if (bundleRef.current) {
                setStaleNotice("Couldn't refresh just now — showing your last results.");
            } else {
                setError(e?.response?.data?.detail || e.message || 'Failed to load priorities');
            }
        } finally {
            setLoading(false);
            onSettledRef.current?.();
        }
    }, [startRunPolling]);

    useEffect(() => { fetchPriorities(false); }, [fetchPriorities]);

    // Load the user's prior ratings once so already-rated cards show their face.
    useEffect(() => {
        let cancelled = false;
        client.get('/api/feedback/mine', { params: { surface: 'priorities' } })
            .then(res => { if (!cancelled) setRatings(res.data || {}); })
            .catch(() => { /* non-fatal — buttons just start unselected */ });
        return () => { cancelled = true; };
    }, []);

    const optimisticRemove = (priorityId: string): PriorityItem | null => {
        if (!bundle) return null;
        const removed = bundle.priorities.find(p => p.id === priorityId) || null;
        setBundle({
            ...bundle,
            priorities: bundle.priorities.filter(p => p.id !== priorityId),
        });
        return removed;
    };

    const restoreCard = (priority: PriorityItem | null) => {
        if (!priority || !bundle) return;
        setBundle({ ...bundle, priorities: [...bundle.priorities, priority] });
    };

    const callAction = async (
        priorityId: string,
        verb: 'dismiss' | 'snooze' | 'resolve',
        body: Record<string, unknown> = {},
    ) => {
        // Recommendations (origin="recommendation") live in agent_recommendations
        // and have their own action endpoints; everything else is a UC-25 priority.
        const item = bundle?.priorities.find(p => p.id === priorityId);
        const path = item?.origin === 'recommendation'
            ? `/api/agents/recommendations/${priorityId}/${verb}`
            : `/api/homefeed/priorities/${priorityId}/${verb}`;
        setPendingAction(priorityId);
        const removed = optimisticRemove(priorityId);
        try {
            await client.post(path, body);
        } catch (e: any) {
            if (e?.response?.status === 404) {
                // The card was regenerated under us (a Refresh / "get more"
                // run gave the priorities new ids). The optimistic removal is the
                // right outcome — don't restore the stale item; just re-sync to the
                // current card with a soft notice instead of a hard red error.
                setStaleNotice('That list was just refreshed — re-syncing…');
                fetchPriorities(false);
            } else {
                restoreCard(removed);
                setError(e?.response?.data?.detail || e.message || `${verb} failed`);
            }
        } finally {
            setPendingAction(null);
        }
    };

    /** Suppress every FUTURE occurrence of a recurring meeting, not just this one.
     *  Confirmed first — it's the one card action the user can't undo from the card. */
    const dismissSeries = (p: PriorityItem) => {
        const ok = window.confirm(
            `Stop surfacing "${p.title}" for every future occurrence of this recurring meeting?\n\n` +
            `You can bring it back from Settings → AI Personalization → reopen dismissed priorities.`,
        );
        if (!ok) return;
        return callAction(p.id, 'dismiss', { scope: 'series' });
    };

    const handleSnoozeDays = (priorityId: string, daysOut: number) => {
        const item = bundle?.priorities.find(p => p.id === priorityId);
        if (item?.origin === 'recommendation') {
            // /agents snooze takes {days}; the priorities endpoint takes {until}.
            return callAction(priorityId, 'snooze', { days: daysOut });
        }
        const until = new Date();
        until.setDate(until.getDate() + daysOut);
        return callAction(priorityId, 'snooze', { until: until.toISOString() });
    };

    const handleCustomSnooze = (priorityId: string) => {
        const input = window.prompt(
            'Snooze until (YYYY-MM-DD or full ISO datetime):',
            new Date(Date.now() + 7 * 86_400_000).toISOString().slice(0, 10),
        );
        if (!input) return;
        const parsed = new Date(input);
        if (isNaN(parsed.getTime())) {
            setError('Could not parse that date');
            return;
        }
        const item = bundle?.priorities.find(p => p.id === priorityId);
        if (item?.origin === 'recommendation') {
            const days = Math.max(
                1, Math.ceil((parsed.getTime() - Date.now()) / 86_400_000),
            );
            return callAction(priorityId, 'snooze', { days });
        }
        return callAction(priorityId, 'snooze', { until: parsed.toISOString() });
    };

    // "Add to my To-Dos" — turn Cubie's suggestion into a promise the user
    // owns. Idempotent per card on the server (a second click says so).
    const addToTodos = async (p: PriorityItem) => {
        try {
            const res = await client.post('/api/todos/from-priority', { priority_id: p.id });
            setSuccessFlash(res.data?.created === false ? 'Already on your To-Dos.' : 'Added to your To-Dos.');
            setTimeout(() => setSuccessFlash(null), 3000);
        } catch (e: unknown) {
            const r = e as { response?: { data?: { detail?: string } }; message?: string } | null;
            setError(r?.response?.data?.detail || r?.message || 'Could not add that to your To-Dos');
        }
    };

    const triggerFocusBlock = async (p: PriorityItem) => {
        try {
            const res = await client.post('/api/calendar/focus-block', {
                title: `Focus: ${p.title}`,
                duration_minutes: 30,
                priority_id: p.id,
            });
            const link = res.data?.html_link;
            if (link) window.open(link, '_blank', 'noopener,noreferrer');
        } catch (e: any) {
            const detail = e?.response?.data?.detail;
            if (detail === 'no_free_slot') {
                setError('No free 30-min slot in the next 24h.');
            } else {
                setError(detail || e.message || 'Focus block failed');
            }
        }
    };

    // ─── EOS Slack follow-up (preview-then-send) ─────────────────────────────
    //
    // Manager taps "Slack follow-up" on a report's about-to-be-due EOS item.
    // Step 1 (open): confirm=false → the backend returns a proposed nudge the
    // manager can edit. Step 2 (send): confirm=true → the DM goes out as the
    // Cubie bot and the backend snoozes the manager's own card 3 days, so a
    // refresh drops it from the list.
    const openSlackFollowup = useCallback((p: PriorityItem) => {
        setSlackModal({
            priorityId: p.id,
            targetLabel: p.target_label || '',
            message: '',
            loading: true,
            sending: false,
            notice: null,
        });
        client
            .post(`/api/homefeed/priorities/${p.id}/slack-followup`, { confirm: false })
            .then((res) => {
                setSlackModal(prev =>
                    prev && prev.priorityId === p.id
                        ? {
                            ...prev,
                            targetLabel: res.data?.target_label || prev.targetLabel,
                            message: res.data?.preview || '',
                            loading: false,
                        }
                        : prev,
                );
            })
            .catch((e: any) => {
                setSlackModal(null);
                setError(
                    e?.response?.data?.detail || e.message || 'Could not prepare the follow-up',
                );
            });
    }, []);

    const sendSlackFollowup = async () => {
        if (!slackModal) return;
        const { priorityId, message } = slackModal;
        if (!message.trim()) return;
        setSlackModal({ ...slackModal, sending: true, notice: null });
        try {
            const res = await client.post(
                `/api/homefeed/priorities/${priorityId}/slack-followup`,
                { confirm: true, message },
            );
            if (res.data?.sent) {
                setSlackModal(null);
                setSuccessFlash('Slack nudge sent.');
                window.setTimeout(() => setSuccessFlash(null), 4000);
                fetchPriorities(false); // backend snoozed the card — refresh drops it
                return;
            }
            const reason = res.data?.reason;
            const friendly =
                reason === 'slack_not_connected'
                    ? "Slack isn't connected."
                    : reason === 'recipient_not_in_workspace'
                        ? "That person isn't in the Slack workspace."
                        : "Couldn't send the nudge — please try again.";
            setSlackModal(prev => (prev ? { ...prev, sending: false, notice: friendly } : prev));
        } catch (e: any) {
            setSlackModal(null);
            setError(e?.response?.data?.detail || e.message || 'Failed to send Slack nudge');
        }
    };

    // ─── "Get me other important things" ─────────────────────────────────────
    //
    // First try the cheap path (overnight recommendations). If the backend has
    // none, it kicks a background pipeline run and we poll the read endpoint
    // until a newer card lands — so the user can leave and come back.
    // (pollRef / stopPolling are declared up top — shared with the UC-41 poller.)

    const startPollingForNewCard = useCallback((baselineGeneratedAt: string) => {
        stopPolling();
        setGenerating(true);
        setNoNewItems(false);
        let polls = 0;
        const MAX_POLLS = 12; // ~3 min at 15s
        pollRef.current = window.setInterval(async () => {
            polls += 1;
            try {
                const res = await client.post<PrioritiesResponse>(
                    '/api/homefeed/priorities',
                    { force_refresh: false },
                );
                const landed = !!res.data.generated_at
                    && res.data.generated_at !== baselineGeneratedAt;
                if (landed) {
                    stopPolling();
                    setGenerating(false);
                    setBundle(res.data);
                    if ((res.data.priorities?.length ?? 0) === 0) setNoNewItems(true);
                    return;
                }
            } catch {
                /* transient — keep polling */
            }
            if (polls >= MAX_POLLS) {
                stopPolling();
                setGenerating(false);
                setError('Still working on it — try Refresh in a moment.');
            }
        }, 15_000);
    }, [stopPolling]);

    const requestMore = useCallback(async () => {
        setMoreLoading(true);
        setError(null);
        setNoNewItems(false);
        const baseline = bundle?.generated_at ?? '';
        try {
            const res = await client.post<MoreResponse>('/api/homefeed/priorities/more');
            if (res.data.status === 'recommendations' && res.data.items.length > 0) {
                setBundle(prev =>
                    prev ? { ...prev, priorities: [...prev.priorities, ...res.data.items] } : prev,
                );
            } else {
                // running | already_running | empty recs — wait for a fresh card.
                startPollingForNewCard(baseline);
            }
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Could not fetch more');
        } finally {
            setMoreLoading(false);
        }
    }, [bundle?.generated_at, startPollingForNewCard]);

    const actionCtx: ActionContext = {
        navigate, triggerFocusBlock, setError, refresh: fetchPriorities,
        openSlackFollowup,
    };

    const remaining = bundle?.priorities.length ?? 0;
    const generatedCount = bundle?.generated_count ?? 0;
    // The user cleared a non-empty list today => celebrate. Distinct from a day
    // where nothing was ever surfaced (generated_count === 0).
    const allCleared = !!bundle && remaining === 0 && generatedCount > 0;

    // PTO banner — when the user is out today, Cubie is holding their day.
    const pto = bundle?.pto?.on_pto ? bundle.pto : null;
    // Calendar hygiene — the meetings deliberately kept OUT of the list above,
    // reported once so nothing goes silently missing.
    const hygiene =
        bundle?.calendar_hygiene && bundle.calendar_hygiene.unanswered_count > 0
            ? bundle.calendar_hygiene
            : null;
    const fmtPtoDay = (iso: string) =>
        new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {
            weekday: 'long',
            month: 'short',
            day: 'numeric',
        });

    return (
        <div data-tour="home-priorities" className="bg-white border border-gray-100 rounded-2xl shadow-sm p-5">
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-amber-500" />
                    <h3 className="text-sm font-semibold text-gray-900">AI Priorities</h3>
                </div>
                <div className="flex items-center gap-2">
                    {/* How old this list is, beside the control that renews it.
                        "Compiled" — not "as of" — because generated_at is when
                        the pipeline FINISHED, not how fresh the underlying
                        email/HubSpot/calendar data is. Per-channel freshness
                        isn't persisted, so a per-source claim would be invented.
                        withZone: readers can sit in different time zones. */}
                    {bundle?.generated_at && !indexing && (
                        <span className="text-xs text-gray-400" title="When Cubie last compiled these priorities">
                            Compiled {formatInstantTime(bundle.generated_at, { withZone: true })}
                        </span>
                    )}
                    <button
                        onClick={() => fetchPriorities(true)}
                        disabled={loading || indexing}
                        className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-amber-700 disabled:opacity-50"
                        title="Re-run the pipeline"
                    >
                        <RefreshCw className={`h-3.5 w-3.5 ${loading || indexing ? 'animate-spin' : ''}`} />
                        {indexing ? 'Indexing…' : 'Refresh'}
                    </button>
                </div>
            </div>

            {pto && (
                <div className="p-3 mb-3 bg-emerald-50 border border-emerald-100 text-emerald-800 rounded-lg text-sm flex items-start gap-2">
                    <span className="text-base leading-none">🌴</span>
                    <span>
                        You&apos;re on PTO through <span className="font-medium">{fmtPtoDay(pto.end)}</span> — back{' '}
                        <span className="font-medium">{fmtPtoDay(pto.return_date)}</span>. Cubie is holding your priorities;
                        enjoy the time off.
                    </span>
                </div>
            )}

            {bundle?.headline && !pto && (
                <div className="text-xs italic text-amber-700 mb-3">
                    {bundle.headline}
                </div>
            )}

            {hygiene && !pto && (
                <div className="p-2.5 mb-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-600 flex items-start gap-2">
                    <CalendarClock className="h-3.5 w-3.5 flex-shrink-0 mt-0.5 text-gray-400" />
                    <div className="min-w-0">
                        <span>
                            {hygiene.unanswered_count === 1
                                ? "1 upcoming invite you haven't answered, so it's not in your priorities:"
                                : `${hygiene.unanswered_count} upcoming invites you haven't answered, so they're not in your priorities:`}
                        </span>
                        <ul className="mt-1 space-y-0.5">
                            {hygiene.invites.map((inv, i) => (
                                <li key={i} className="truncate">
                                    {inv.link ? (
                                        <a
                                            href={inv.link}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="font-medium text-gray-700 hover:text-brand hover:underline"
                                        >
                                            {inv.title}
                                        </a>
                                    ) : (
                                        <span className="font-medium text-gray-700">{inv.title}</span>
                                    )}
                                    {inv.when_local && <span className="text-gray-400"> · {inv.when_local}</span>}
                                </li>
                            ))}
                            {hygiene.unanswered_count > hygiene.invites.length && (
                                <li className="text-gray-400">
                                    and {hygiene.unanswered_count - hygiene.invites.length} more
                                </li>
                            )}
                        </ul>
                        <span className="text-gray-400">
                            Accept or decline and they&apos;ll stop showing up here.
                        </span>
                    </div>
                </div>
            )}

            {error && (
                <div className="p-2.5 mb-3 bg-red-50 text-red-700 rounded-md flex items-center gap-2 text-xs">
                    <AlertCircle className="h-4 w-4" /> {error}
                </div>
            )}

            {staleNotice && !error && (
                <div className="p-2.5 mb-3 bg-amber-50 text-amber-700 rounded-md flex items-center gap-2 text-xs">
                    <AlertCircle className="h-4 w-4" /> {staleNotice}
                </div>
            )}

            {successFlash && (
                <div className="p-2.5 mb-3 bg-green-50 text-green-700 rounded-md flex items-center gap-2 text-xs">
                    <Check className="h-4 w-4" /> {successFlash}
                </div>
            )}

            {indexing && !bundle ? (
                <div className="py-8 text-center flex flex-col items-center gap-2">
                    <RefreshCw className="h-5 w-5 animate-spin text-amber-500" />
                    <p className="text-sm font-medium text-gray-700">Indexing your priorities…</p>
                    <p className="text-xs text-gray-400 max-w-xs mx-auto">
                        Scanning your channels — usually about 2 minutes. You can leave this
                        page and come back; it updates on its own.
                    </p>
                </div>
            ) : loading && !bundle ? (
                <div className="flex items-center justify-center py-8">
                    <RefreshCw className="h-5 w-5 animate-spin text-gray-400" />
                </div>
            ) : !bundle || bundle.priorities.length === 0 ? (
                <div className="py-6 text-center">
                    {generating ? (
                        <div className="flex flex-col items-center gap-2">
                            <RefreshCw className="h-5 w-5 animate-spin text-amber-500" />
                            <p className="text-sm font-medium text-gray-700">Working on it…</p>
                            <p className="text-xs text-gray-400 max-w-xs mx-auto">
                                Scanning your channels for more. This can take a minute — you can
                                leave this page and come back; the list updates on its own.
                            </p>
                        </div>
                    ) : (
                        <>
                            {allCleared ? (
                                <>
                                    <div className="text-2xl mb-1">🎉</div>
                                    <p className="text-sm font-semibold text-gray-900">{kudosPhrase}</p>
                                </>
                            ) : noNewItems ? (
                                <p className="text-sm text-gray-500">
                                    You're all caught up — nothing new right now.
                                </p>
                            ) : (
                                <p className="text-xs text-gray-400 italic">No priorities surfaced today.</p>
                            )}
                            <button
                                onClick={requestMore}
                                disabled={moreLoading}
                                className="mt-4 inline-flex items-center gap-1.5 text-xs font-medium text-amber-700 bg-amber-50 hover:bg-amber-100 px-3 py-1.5 rounded-lg disabled:opacity-50"
                            >
                                {moreLoading
                                    ? <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                                    : <Sparkles className="h-3.5 w-3.5" />}
                                Get me other important things
                            </button>
                        </>
                    )}
                </div>
            ) : (
                <ul className="divide-y divide-gray-50">
                    {bundle.priorities.map((p, idx) => {
                        const urgencyClass =
                            p.urgency === 'high' ? 'bg-red-100 text-red-700' :
                            p.urgency === 'low'  ? 'bg-gray-100 text-gray-600' :
                                                   'bg-amber-100 text-amber-700';
                        const gmailThread = p.entity_kind === 'thread' && (
                            p.source_channels?.includes('gmail')
                            || p.source_links?.[0]?.startsWith('https://mail.google.com/mail/')
                        );
                        const firstLink = gmailThread
                            ? priorityThreadUrl(p.attrs, p.entity_id)
                            : p.entity_kind === 'person' && p.attrs?.private_gmail_context
                                ? null
                                : p.source_links?.[0];
                        const isPending = pendingAction === p.id;
                        const isExpanded = !!expanded[p.id];
                        const verb = entityAction(p);
                        const isRec = p.origin === 'recommendation';
                        const isNextWeek = p.origin === 'next_week';
                        // Next-week items are appended after the daily ones, so
                        // the first one gets a subtle "Looking ahead" divider.
                        const isFirstNextWeek =
                            isNextWeek && bundle.priorities[idx - 1]?.origin !== 'next_week';
                        return (
                            <React.Fragment key={p.id}>
                            {isFirstNextWeek && (
                                <li className="pt-3 pb-1 first:pt-0" aria-hidden="true">
                                    <span className="text-[10px] font-semibold uppercase tracking-wide text-brand">
                                        Looking ahead — next week
                                    </span>
                                </li>
                            )}
                            <li data-tour={idx === 0 ? 'home-priority-card' : undefined} className="py-3 first:pt-0 last:pb-0">
                                <div
                                    role="button"
                                    tabIndex={0}
                                    aria-expanded={isExpanded}
                                    onClick={() => setExpanded({ ...expanded, [p.id]: !isExpanded })}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter' || e.key === ' ') {
                                            e.preventDefault();
                                            setExpanded({ ...expanded, [p.id]: !isExpanded });
                                        }
                                    }}
                                    className="flex items-start justify-between gap-2 cursor-pointer select-none rounded -mx-1 px-1 hover:bg-amber-50/40"
                                    title={isExpanded ? 'Click to collapse' : 'Click to see options'}
                                >
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2 mb-1">
                                            <span className={`text-[10px] font-semibold uppercase tracking-tight px-1.5 py-0.5 rounded ${urgencyClass}`}>
                                                {p.urgency}
                                            </span>
                                            {isRec && (
                                                <span className="text-[10px] font-medium text-indigo-600 bg-indigo-50 px-1.5 py-0.5 rounded">
                                                    Overnight scan
                                                </span>
                                            )}
                                            {isNextWeek && (
                                                <span
                                                    className="text-[10px] font-medium text-brand-hover bg-brand-tint px-1.5 py-0.5 rounded"
                                                    title={p.week_of ? `Next week report — week of ${p.week_of}` : 'Next week report'}
                                                >
                                                    Next week
                                                </span>
                                            )}
                                            {p.attrs?.gmail_mailbox && (
                                                <span
                                                    className="max-w-[18rem] truncate rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700"
                                                    title={`Gmail mailbox: ${p.attrs.gmail_mailbox}`}
                                                    aria-label={`Gmail mailbox: ${p.attrs.gmail_mailbox}`}
                                                >
                                                    {p.attrs.gmail_mailbox}
                                                </span>
                                            )}
                                            <p className="text-sm font-semibold text-gray-900 leading-snug">
                                                {p.title}
                                            </p>
                                            {firstLink && (
                                                <a
                                                    href={firstLink}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    onClick={(e) => e.stopPropagation()}
                                                    className="text-gray-300 hover:text-amber-500"
                                                >
                                                    <ExternalLink className="h-3.5 w-3.5" />
                                                </a>
                                            )}
                                        </div>
                                        {p.why_it_matters && (
                                            <p className="text-xs text-gray-600 leading-snug mb-1">
                                                {p.why_it_matters}
                                            </p>
                                        )}
                                        {p.suggested_action && (
                                            <p className="text-xs text-amber-700 leading-snug">
                                                <span className="font-semibold">Next:</span> {p.suggested_action}
                                            </p>
                                        )}
                                        {p.priority_kind === KIND_PROMISE_UNKEPT && <PromiseSaidStrip p={p} />}
                                        {p.source_channels?.length > 0 && (
                                            <div className="flex items-center gap-1.5 mt-1.5">
                                                {p.source_channels.map(c => {
                                                    const isEos = c.toLowerCase() === 'eos';
                                                    // Inherited = handed over by a departed teammate
                                                    // (offboard handoff) — amber so it reads as
                                                    // "someone else's work, now yours".
                                                    const isInherited = c.toLowerCase() === 'inherited';
                                                    return (
                                                        <span
                                                            key={c}
                                                            className={`text-[10px] uppercase tracking-tight px-1.5 py-0.5 rounded ${
                                                                isEos
                                                                    ? 'text-indigo-600 bg-indigo-50'
                                                                    : isInherited
                                                                        ? 'text-amber-700 bg-amber-50'
                                                                        : 'text-gray-500 bg-gray-100'
                                                            }`}
                                                        >
                                                            {c}
                                                        </span>
                                                    );
                                                })}
                                            </div>
                                        )}
                                    </div>
                                    <div className="flex items-center gap-1 flex-shrink-0">
                                        {!isRec && (
                                            <button
                                                onClick={(e) => { e.stopPropagation(); callAction(p.id, 'resolve'); }}
                                                disabled={isPending}
                                                title="Mark done"
                                                className="p-1.5 text-gray-500 hover:text-green-700 hover:bg-green-50 rounded disabled:opacity-50"
                                            >
                                                <Check className="h-3.5 w-3.5" />
                                            </button>
                                        )}
                                        {/* Recurring meeting → offer the series-wide
                                            suppression explicitly. Dismissing one
                                            occurrence can't silence next week's, and
                                            widening ✗ to do that silently would make
                                            one click far more destructive than it looks. */}
                                        {!isRec && p.series_entity_id && (
                                            <button
                                                onClick={(e) => { e.stopPropagation(); dismissSeries(p); }}
                                                disabled={isPending}
                                                title="Never prep this recurring meeting again"
                                                className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-50"
                                            >
                                                <CalendarX className="h-3.5 w-3.5" />
                                            </button>
                                        )}
                                        <button
                                            onClick={(e) => { e.stopPropagation(); callAction(p.id, 'dismiss'); }}
                                            disabled={isPending}
                                            title={p.series_entity_id ? 'Dismiss just this one' : 'Dismiss'}
                                            className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-50"
                                        >
                                            <X className="h-3.5 w-3.5" />
                                        </button>
                                    </div>
                                </div>

                                {/* Feedback is exportable to admins; never copy private Gmail context there. */}
                                {!p.attrs?.private_gmail_context && !p.attrs?.personal_gmail && (
                                <div className="mt-2" onClick={(e) => e.stopPropagation()}>
                                    <FeedbackButtons
                                        surface="priorities"
                                        proposalId={p.id}
                                        proposalText={[p.title, p.why_it_matters, p.suggested_action ? `Next: ${p.suggested_action}` : '']
                                            .filter(Boolean).join('\n')}
                                        proposalMetadata={{
                                            title: p.title,
                                            urgency: p.urgency,
                                            suggested_action: p.suggested_action,
                                            priority_kind: p.priority_kind,
                                            entity_kind: p.entity_kind,
                                            entity_id: p.entity_id,
                                            origin: p.origin || 'priority',
                                        }}
                                        initialRating={(ratings[p.id]?.rating as FeedbackRating) || null}
                                        initialComment={ratings[p.id]?.comment || null}
                                    />
                                </div>
                                )}

                                {isExpanded && (
                                    <div className="mt-3 pt-3 border-t border-gray-100 grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
                                        {/* Snooze options */}
                                        <div className="space-y-1">
                                            <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-tight mb-1 inline-flex items-center gap-1">
                                                <Clock size={11} /> Snooze
                                            </div>
                                            <SnoozeBtn onClick={() => handleSnoozeDays(p.id, 1)} label="Tomorrow" disabled={isPending} />
                                            <SnoozeBtn onClick={() => handleSnoozeDays(p.id, 3)} label="In 3 days" disabled={isPending} />
                                            <SnoozeBtn onClick={() => handleSnoozeDays(p.id, 7)} label="Next week" disabled={isPending} />
                                            <SnoozeBtn onClick={() => handleCustomSnooze(p.id)} label="Custom…" disabled={isPending} />
                                        </div>
                                        {/* Schedule focus (priorities only — recs have no focus-block path) */}
                                        {!isRec && (
                                            <div className="space-y-1">
                                                <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-tight mb-1 inline-flex items-center gap-1">
                                                    <CalendarPlus size={11} /> Schedule
                                                </div>
                                                <SnoozeBtn
                                                    onClick={() => triggerFocusBlock(p)}
                                                    label="30 min focus block"
                                                    disabled={isPending}
                                                />
                                            </div>
                                        )}
                                        {/* Entity-specific verb. */}
                                        <div className="space-y-1">
                                            <div className="text-[10px] font-semibold text-gray-400 uppercase tracking-tight mb-1 inline-flex items-center gap-1">
                                                <Briefcase size={11} />
                                                Act
                                            </div>
                                            {canAccessTodos && p.entity_kind !== 'promise' && (
                                                <SnoozeBtn
                                                    onClick={() => addToTodos(p)}
                                                    label="Add to my To-Dos"
                                                    disabled={isPending}
                                                />
                                            )}
                                            {canDefer && !isRec && p.entity_kind === 'deal' && p.entity_id && (
                                                <SnoozeBtn
                                                    onClick={() =>
                                                        setDeferTarget({
                                                            deal_id: String(p.entity_id),
                                                            deal_name: p.entity_label ?? null,
                                                        })
                                                    }
                                                    label="Defer the deal…"
                                                    disabled={isPending}
                                                />
                                            )}
                                            {verb ? (
                                                <button
                                                    onClick={() => verb.onClick(p, actionCtx)}
                                                    disabled={isPending}
                                                    className="w-full text-left px-2 py-1.5 hover:bg-amber-50 rounded inline-flex items-center gap-1.5 disabled:opacity-50"
                                                >
                                                    {verb.icon}
                                                    {verb.label}
                                                </button>
                                            ) : (
                                                <div className="text-[11px] text-gray-400 italic px-2 py-1.5">
                                                    No additional actions.
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                )}

                            </li>
                            </React.Fragment>
                        );
                    })}
                </ul>
            )}

            <DeferDealModal
                key={deferTarget?.deal_id ?? 'closed'}
                target={deferTarget}
                onClose={() => setDeferTarget(null)}
                onDeferred={(res) => {
                    // Every card about this deal goes; the server drops them on
                    // the next read too, so a reload agrees.
                    const dealId = res.defer.deal_id;
                    const current = bundleRef.current;
                    if (current) {
                        setBundle({
                            ...current,
                            priorities: current.priorities.filter(
                                x => !(x.entity_kind === 'deal' && String(x.entity_id) === dealId),
                            ),
                        });
                    }
                }}
            />

            {slackModal && (
                <SlackFollowupModal
                    state={slackModal}
                    onChange={(msg) =>
                        setSlackModal(prev => (prev ? { ...prev, message: msg } : prev))
                    }
                    onCancel={() => setSlackModal(null)}
                    onSend={sendSlackFollowup}
                />
            )}
        </div>
    );
};


// Preview-then-send Slack nudge dialog. Minimal overlay, styled to match the
// panel (white rounded card, amber accents). The recipient is fixed by the
// backend authz check; only the message body is editable.
const SlackFollowupModal: React.FC<{
    state: SlackFollowupState;
    onChange: (msg: string) => void;
    onCancel: () => void;
    onSend: () => void;
}> = ({ state, onChange, onCancel, onSend }) => (
    <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
        onClick={onCancel}
    >
        <div
            className="bg-white rounded-2xl shadow-xl w-full max-w-md p-5"
            role="dialog"
            aria-modal="true"
            onClick={(e) => e.stopPropagation()}
        >
            <div className="flex items-center gap-2 mb-1">
                <MessageSquare className="h-4 w-4 text-amber-500" />
                <h3 className="text-sm font-semibold text-gray-900">Slack follow-up</h3>
            </div>
            <p className="text-xs text-gray-500 mb-3">
                To <span className="font-medium text-gray-700">{state.targetLabel || 'your report'}</span>
            </p>
            {state.loading ? (
                <div className="flex items-center justify-center py-8">
                    <RefreshCw className="h-5 w-5 animate-spin text-gray-400" />
                </div>
            ) : (
                <>
                    <textarea
                        value={state.message}
                        onChange={(e) => onChange(e.target.value)}
                        rows={5}
                        disabled={state.sending}
                        className="w-full text-sm text-gray-800 border border-gray-200 rounded-lg p-2.5 focus:outline-none focus:ring-1 focus:ring-amber-400 resize-none disabled:opacity-60"
                        placeholder="Write a quick nudge…"
                    />
                    {state.notice && (
                        <div className="mt-2 p-2 bg-amber-50 text-amber-700 rounded-md text-xs flex items-center gap-1.5">
                            <AlertCircle className="h-3.5 w-3.5 flex-shrink-0" /> {state.notice}
                        </div>
                    )}
                    <div className="mt-4 flex items-center justify-end gap-2">
                        <button
                            onClick={onCancel}
                            disabled={state.sending}
                            className="px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100 rounded-lg disabled:opacity-50"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={onSend}
                            disabled={state.sending || !state.message.trim()}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-amber-600 hover:bg-amber-700 rounded-lg disabled:opacity-50"
                        >
                            {state.sending
                                ? <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                                : <MessageSquare className="h-3.5 w-3.5" />}
                            Send Slack nudge
                        </button>
                    </div>
                </>
            )}
        </div>
    </div>
);

// ─── "You said you would…" strip ────────────────────────────────────────────
//
// The reader's own words, when and where, and exactly what Cubie looked at.
// "No sign of it in sent email and Slack" is a statement with a denominator;
// it is never "you didn't do it" — calls and other tools are invisible to
// Cubie, and the ✓ is how the reader says it happened anyway.
const PromiseSaidStrip: React.FC<{ p: PriorityItem }> = ({ p }) => {
    const a = p.attrs;
    if (!a?.promise_quote) return null;
    const when = a.promise_said_on ? formatSaidOn(a.promise_said_on) : null;
    return (
        <div className="mt-1.5 space-y-0.5">
            <p className="border-l-2 border-gray-200 pl-2 text-[12px] italic leading-snug text-gray-700">
                “{a.promise_quote}”
            </p>
            <p className="text-[11px] leading-snug text-gray-500">
                {[when && `You said this ${when}`, a.promise_where && `in ${a.promise_where}`]
                    .filter(Boolean)
                    .join(' ')}
                {a.promise_checked && <>. No sign of it yet in your {a.promise_checked}.</>}
            </p>
        </div>
    );
};

// A bare calendar date ("2026-09-24") read as a local day, never as a UTC
// instant — parsing it with `new Date()` would land it a day early west of
// Greenwich.
function formatSaidOn(iso: string): string {
    const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
    if (!y || !m || !d) return iso;
    return 'on ' + new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

const SnoozeBtn: React.FC<{ onClick: () => void; label: string; disabled?: boolean }> = ({
    onClick, label, disabled,
}) => (
    <button
        onClick={onClick}
        disabled={disabled}
        className="w-full text-left px-2 py-1.5 hover:bg-amber-50 rounded text-gray-700 disabled:opacity-50"
    >
        {label}
    </button>
);

export default AIPrioritiesPanel;
