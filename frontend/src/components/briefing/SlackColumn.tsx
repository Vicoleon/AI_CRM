// Slack activity tab for the Productivity route (UC-15) — "unified card feed".
// A compact rail of channels with unread (hover a chip for an on-demand AI
// summary of its unread messages), then a single recency-sorted feed of
// everything aimed at YOU: unread @-mentions + unread DMs. Everything here is
// already unread-scoped by the backend (read items are dropped), so the feed is
// "still needs you", not "recent history".
import React, { useCallback, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Hash, Lock, MessageSquare, Slack, Sparkles } from 'lucide-react';
import client from '../../api/client';
import type {
    SlackActivity,
    SlackActivityChannel,
    SlackActivityDM,
    SlackActivityMention,
} from './useSlackActivity';


// ─── Slack mrkdwn rendering ──────────────────────────────────────────────────
// Raw Slack text carries tokens like <@U123|Jordan Lee>, <#C1|general>,
// <!here>, and <https://x|label>, plus HTML-escaped &amp; / &lt; / &gt;. Render
// the tokens as readable chips/text and decode the entities — never leak the
// raw <@U…> markup the way the first cut did.
const ENTITIES: Record<string, string> = {
    '&amp;': '&',
    '&lt;': '<',
    '&gt;': '>',
    '&quot;': '"',
    '&#39;': "'",
};

function decodeEntities(s: string): string {
    return s.replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g, m => ENTITIES[m] || m);
}

function renderSlackText(raw: string): React.ReactNode[] {
    const nodes: React.ReactNode[] = [];
    const re = /<([^>]+)>/g;
    let last = 0;
    let key = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(raw)) !== null) {
        if (m.index > last) nodes.push(decodeEntities(raw.slice(last, m.index)));
        const inner = m[1];
        const label = inner.includes('|') ? inner.split('|').slice(1).join('|') : '';
        if (inner.startsWith('@') || inner.startsWith('!')) {
            const fallback = inner.startsWith('!') ? inner.slice(1) : 'mention';
            nodes.push(
                <span
                    key={key++}
                    className="bg-brand-soft/60 text-brand-hover rounded px-1 font-medium whitespace-nowrap"
                >
                    @{decodeEntities(label || fallback)}
                </span>,
            );
        } else if (inner.startsWith('#')) {
            const name = label || inner.slice(1);
            nodes.push(
                <span key={key++} className="text-brand-hover font-medium">
                    #{decodeEntities(name)}
                </span>,
            );
        } else {
            // link <url> or <url|label>
            nodes.push(decodeEntities(label || inner.split('|')[0]));
        }
        last = re.lastIndex;
    }
    if (last < raw.length) nodes.push(decodeEntities(raw.slice(last)));
    return nodes;
}


// ─── Avatars & time ──────────────────────────────────────────────────────────
// Categorical hues, not the brand: avatars only need to tell people apart, so
// they keep fixed colours when a workspace changes its accent.
const AVATAR_COLORS = [
    'bg-violet-100 text-violet-700',
    'bg-blue-100 text-blue-700',
    'bg-emerald-100 text-emerald-700',
    'bg-amber-100 text-amber-700',
    'bg-rose-100 text-rose-700',
    'bg-teal-100 text-teal-700',
];

function colorFor(key: string): string {
    let h = 0;
    for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
    return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function initials(name: string): string {
    const parts = name.trim().split(/\s+/);
    return (((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase()) || '?';
}

function timeAgo(ts: string): string {
    const seconds = parseFloat(ts);
    if (!Number.isFinite(seconds) || seconds === 0) return '';
    const mins = Math.round((Date.now() - seconds * 1000) / 60_000);
    if (mins < 1) return 'now';
    if (mins < 60) return `${mins}m`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h`;
    return `${Math.round(hours / 24)}d`;
}

// A search result in a DM channel surfaces its raw ID as the "channel name" —
// don't render "#D01ABC" as if it were a channel.
function looksLikeId(name?: string | null): boolean {
    return !!name && /^[CDG][A-Z0-9]{6,}$/.test(name);
}


// ─── Unified feed item ───────────────────────────────────────────────────────
type FeedItem =
    | { kind: 'mention'; ts: number; data: SlackActivityMention }
    | { kind: 'dm'; ts: number; data: SlackActivityDM };


const FeedCard: React.FC<{ item: FeedItem }> = ({ item }) => {
    if (item.kind === 'mention') {
        const m = item.data;
        const href = m.permalink || m.app_link || undefined;
        const name = m.user_name || 'Someone';
        const showChannel = m.channel_name && !looksLikeId(m.channel_name);
        const inner = (
            <>
                <div className={`w-9 h-9 rounded-full flex items-center justify-center text-xs font-semibold flex-shrink-0 ${colorFor(name)}`}>
                    {initials(name)}
                </div>
                <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                        <span className="text-sm font-medium text-gray-900 truncate">{name}</span>
                        {showChannel && <span className="text-xs text-gray-400 truncate">#{m.channel_name}</span>}
                        <span className="ml-auto text-xs text-gray-400 flex-shrink-0">{timeAgo(m.ts)}</span>
                    </div>
                    <p className="text-[13px] text-gray-600 mt-0.5 line-clamp-2 leading-snug">
                        {renderSlackText(m.text)}
                    </p>
                </div>
            </>
        );
        return wrapCard(inner, href, `${m.channel_id}-${m.ts}`);
    }
    const d = item.data;
    const inner = (
        <>
            <div className={`w-9 h-9 rounded-full flex items-center justify-center text-xs font-semibold flex-shrink-0 ${colorFor(d.counterparty_name)}`}>
                {initials(d.counterparty_name)}
            </div>
            <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                    <span className="text-sm font-medium text-gray-900 truncate">{d.counterparty_name}</span>
                    <span className="text-xs text-brand flex-shrink-0">{d.is_group ? 'group DM' : 'DM'}</span>
                    <span className="ml-auto flex items-center gap-2 flex-shrink-0">
                        <span className="text-[11px] font-semibold text-brand-hover bg-brand-soft/60 rounded-full px-2 py-0.5">
                            {d.unread_count} new
                        </span>
                        <span className="text-xs text-gray-400">{timeAgo(d.ts)}</span>
                    </span>
                </div>
                {d.preview && (
                    <p className="text-[13px] text-gray-600 mt-0.5 line-clamp-2 leading-snug">
                        {renderSlackText(d.preview)}
                    </p>
                )}
            </div>
        </>
    );
    return wrapCard(inner, d.app_link || undefined, d.channel_id);
};

function wrapCard(inner: React.ReactNode, href: string | undefined, key: string): React.ReactElement {
    const cls = 'flex gap-3 rounded-xl border border-gray-100 p-3 transition-colors';
    return href ? (
        <a key={key} href={href} target="_blank" rel="noopener noreferrer" className={`${cls} hover:bg-brand-tint/60 hover:border-brand-soft/60`}>
            {inner}
        </a>
    ) : (
        <div key={key} className={cls}>{inner}</div>
    );
}


// ─── Channel chip with on-demand hover summary ───────────────────────────────
// Renders the per-channel summary markdown. Allow the slack:// scheme
// (ReactMarkdown strips it by default) so deep-links open the desktop app.
const SlackMarkdown: React.FC<{ md: string }> = ({ md }) => (
    <div className="prose prose-sm max-w-none text-gray-700 prose-p:my-1 prose-ul:my-1 prose-ul:pl-4 prose-li:my-0.5 prose-headings:mt-2.5 prose-headings:mb-1 prose-headings:text-[13px] prose-headings:font-semibold prose-headings:text-gray-800 prose-strong:font-semibold">
        <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            urlTransform={(url) => {
                if (url.startsWith('slack://')) return url;
                return /^(https?:|mailto:|tel:|#|\/)/i.test(url) ? url : '';
            }}
            components={{
                a: ({ node, ...props }) => (
                    <a
                        {...props}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-brand no-underline hover:underline"
                    />
                ),
            }}
        >
            {md}
        </ReactMarkdown>
    </div>
);

interface ChannelSummaryResponse {
    summary_md: string;
    unread_count: number;
    error?: string | null;
}

// A channel chip that, on hover, lazily fetches and shows an AI summary of just
// that channel's unread messages. The fetch fires once per chip (cached after),
// so we never summarize anything the user doesn't actually look at. The popover
// renders in a portal with fixed positioning so it's never clipped by the
// tab card's `overflow-hidden` / the scroll container.
const POPOVER_WIDTH = 320; // px — matches w-80
const ChannelChip: React.FC<{ channel: SlackActivityChannel }> = ({ channel }) => {
    const [open, setOpen] = useState(false);
    const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);
    const [summary, setSummary] = useState<string | null>(null);
    const [status, setStatus] = useState<'idle' | 'loading' | 'error'>('idle');
    const requested = useRef(false);
    const wrapRef = useRef<HTMLDivElement>(null);
    const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    const load = useCallback(async () => {
        if (requested.current) return;
        requested.current = true;
        setStatus('loading');
        try {
            const res = await client.get<ChannelSummaryResponse>(
                `/api/slack/channels/${channel.id}/summary`,
            );
            if (res.data.error) {
                setStatus('error');
            } else {
                setSummary(res.data.summary_md || '_Nothing notable in the unread messages._');
                setStatus('idle');
            }
        } catch {
            setStatus('error');
        }
    }, [channel.id]);

    const openPopover = useCallback(() => {
        if (closeTimer.current) {
            clearTimeout(closeTimer.current);
            closeTimer.current = null;
        }
        const el = wrapRef.current;
        if (el) {
            const r = el.getBoundingClientRect();
            // Clamp so the popover never runs off the right edge of the viewport.
            const left = Math.min(r.left, window.innerWidth - POPOVER_WIDTH - 12);
            setCoords({ top: r.bottom + 6, left: Math.max(12, left) });
        }
        setOpen(true);
        load();
    }, [load]);

    const scheduleClose = useCallback(() => {
        closeTimer.current = setTimeout(() => setOpen(false), 120);
    }, []);

    const label = channel.name || channel.id;
    const chipInner = (
        <span className="inline-flex items-center gap-1.5 text-[13px] text-gray-700">
            {channel.is_private ? <Lock size={12} className="text-gray-400" /> : <Hash size={12} className="text-gray-400" />}
            {label}
            <span className="text-[11px] font-semibold text-brand-hover bg-brand-soft/60 rounded-full px-1.5">
                {channel.unread_count}
            </span>
        </span>
    );
    const chipCls = 'rounded-full border border-gray-200 px-3 py-1.5 transition-colors hover:border-brand-soft hover:bg-brand-tint/60';

    return (
        <div ref={wrapRef} className="relative" onMouseEnter={openPopover} onMouseLeave={scheduleClose}>
            {channel.app_link ? (
                <a href={channel.app_link} className={chipCls}>{chipInner}</a>
            ) : (
                <span className={chipCls}>{chipInner}</span>
            )}

            {open && coords && createPortal(
                <div
                    style={{ position: 'fixed', top: coords.top, left: coords.left, width: POPOVER_WIDTH }}
                    className="z-50 rounded-xl border border-gray-200 bg-white p-3.5 shadow-lg"
                    onMouseEnter={openPopover}
                    onMouseLeave={scheduleClose}
                >
                    <div className="flex items-center gap-1.5 mb-2 text-[11px] font-semibold uppercase tracking-wide text-brand-hover">
                        <Sparkles size={12} /> AI summary · {label}
                    </div>
                    {status === 'loading' && (
                        <div className="space-y-1.5 animate-pulse" aria-label="Summarizing unread messages">
                            <div className="h-2.5 bg-brand-soft/50 rounded w-11/12" />
                            <div className="h-2.5 bg-brand-soft/50 rounded w-4/5" />
                            <div className="h-2.5 bg-brand-soft/50 rounded w-3/4" />
                        </div>
                    )}
                    {status === 'error' && (
                        <p className="text-[13px] text-gray-500">Couldn’t summarize this channel right now.</p>
                    )}
                    {status === 'idle' && summary && <SlackMarkdown md={summary} />}
                </div>,
                document.body,
            )}
        </div>
    );
};


// ─── Column ──────────────────────────────────────────────────────────────────
export const SlackColumn: React.FC<{
    activity?: SlackActivity | null;
    loading?: boolean;
}> = ({ activity, loading }) => {
    if (loading) {
        return (
            <div className="animate-pulse space-y-3 p-4">
                {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className={`h-12 bg-gray-100 rounded-xl ${i % 2 === 0 ? 'w-full' : 'w-11/12'}`} />
                ))}
            </div>
        );
    }

    if (!activity || !activity.is_connected) {
        return (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
                <Slack size={32} className="mb-3 opacity-50" />
                <p className="text-sm text-gray-500">Slack isn’t connected yet.</p>
                <a
                    href="/settings?tab=slack"
                    className="mt-3 inline-flex items-center gap-2 px-4 py-2 bg-brand text-white text-sm font-medium rounded-lg hover:bg-brand-hover transition-colors shadow-sm"
                >
                    <Slack size={15} /> Connect Slack
                </a>
            </div>
        );
    }

    if (activity.error) {
        return (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
                <Slack size={32} className="mb-3 opacity-50" />
                <p className="text-sm text-gray-500">Couldn’t reach Slack right now.</p>
                <a href="/settings?tab=slack" className="mt-2 text-xs text-brand hover:underline">
                    Reconnect Slack →
                </a>
            </div>
        );
    }

    const { mentions, dms, channels } = activity;

    const feed: FeedItem[] = [
        ...mentions.map((m): FeedItem => ({ kind: 'mention', ts: parseFloat(m.ts) || 0, data: m })),
        ...dms.map((d): FeedItem => ({ kind: 'dm', ts: parseFloat(d.ts) || 0, data: d })),
    ].sort((a, b) => b.ts - a.ts);

    if (feed.length === 0 && channels.length === 0) {
        return (
            <div className="flex flex-col items-center justify-center py-16 text-gray-400">
                <MessageSquare size={32} className="mb-2 opacity-50" />
                <p className="text-sm">You’re all caught up on Slack 🎉</p>
            </div>
        );
    }

    return (
        <div className="p-4 space-y-5">
            {/* Channels with unread — a compact rail; hover a chip for an
                on-demand AI summary of that channel's unread messages. */}
            {channels.length > 0 && (
                <div>
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-2">
                        Unread channels
                        <span className="ml-2 normal-case font-normal text-gray-400">— hover for an AI summary</span>
                    </p>
                    <div className="flex flex-wrap gap-2">
                        {channels.map(c => (
                            <ChannelChip key={c.id} channel={c} />
                        ))}
                    </div>
                </div>
            )}

            {/* For you — unified mentions + DMs feed (already shows message text). */}
            {feed.length > 0 && (
                <div>
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500 mb-2">
                        For you
                    </p>
                    <div className="space-y-2">
                        {feed.map(item => (
                            <FeedCard key={item.kind === 'mention' ? `m-${item.data.channel_id}-${item.data.ts}` : `d-${item.data.channel_id}`} item={item} />
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
};

export default SlackColumn;
