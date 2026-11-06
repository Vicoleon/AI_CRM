// The notification bell in the live sidebar (components/Layout.tsx).
//
// Every unread in-app notification, newest first — a meeting prep that
// finished building, a scheduled task's result, a task that paused itself.
// Clicking one opens what it links to and marks it read; "Mark all as read"
// clears the rows listed here (by id — one that lands meanwhile stays unread). The nav's own dots (Chat, Scheduled tasks) keep working:
// they read the same feed.
//
// The sidebar clips its content (`overflow-hidden`, it animates its width),
// so the panel is portalled to <body> and placed beside the bell with fixed
// coordinates. It is a non-modal popover: Esc or a click outside closes it and
// hands focus back to the bell; focus moves into it on open.
import React, { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, Bell, CalendarClock, CheckCheck, CheckCircle2, FileText, Info } from 'lucide-react';

import type { AppNotification } from '../../api/notifications';
import { useNotifications } from '../../context/NotificationsContext';
import { MEETING_PREP_FAILED, MEETING_PREP_READY, badgeLabel, relativeTime } from './rules';

const PANEL_WIDTH = 352;
const GAP = 8;

/** The glyph for a row: what it is about, tinted by how much it matters. */
const rowIcon = (n: AppNotification) => {
    if (n.kind === MEETING_PREP_FAILED) return { Icon: AlertTriangle, tone: 'bg-amber-50 text-amber-600' };
    if (n.kind === MEETING_PREP_READY) return { Icon: FileText, tone: 'bg-brand/10 text-brand' };
    if (n.kind === 'scheduled_task_result') {
        return n.severity === 'info'
            ? { Icon: CalendarClock, tone: 'bg-brand/10 text-brand' }
            : { Icon: AlertTriangle, tone: 'bg-amber-50 text-amber-600' };
    }
    if (n.severity === 'urgent') return { Icon: AlertTriangle, tone: 'bg-red-50 text-red-600' };
    if (n.severity === 'warn') return { Icon: AlertTriangle, tone: 'bg-amber-50 text-amber-600' };
    return { Icon: Info, tone: 'bg-slate-100 text-slate-500' };
};

interface Anchor {
    left: number;
    bottom: number;
    maxHeight: number;
}

export const NotificationBell: React.FC<{
    /** The nav panel is open, so the bell has room for its label. */
    expanded: boolean;
}> = ({ expanded }) => {
    const { unread, markRead, markAllRead } = useNotifications();
    const navigate = useNavigate();
    const [anchor, setAnchor] = useState<Anchor | null>(null);
    const open = anchor !== null;
    const buttonRef = useRef<HTMLButtonElement>(null);
    const panelRef = useRef<HTMLDivElement>(null);
    const panelId = useId();
    const headingId = useId();

    const count = unread.length;
    const badge = badgeLabel(count);
    const label = count ? `Notifications, ${count} unread` : 'Notifications';

    const close = (returnFocus: boolean) => {
        setAnchor(null);
        if (returnFocus) buttonRef.current?.focus();
    };

    const toggle = () => {
        if (open) {
            close(false);
            return;
        }
        const r = buttonRef.current?.getBoundingClientRect();
        if (!r) return;
        // Beside the bell, bottom-aligned with it, never off the right edge.
        const left = Math.max(GAP, Math.min(r.right + GAP, window.innerWidth - PANEL_WIDTH - GAP));
        const bottom = Math.max(GAP, window.innerHeight - r.bottom);
        setAnchor({ left, bottom, maxHeight: Math.max(240, window.innerHeight - bottom - GAP * 2) });
    };

    useEffect(() => {
        if (!open) return;
        panelRef.current?.focus();
        const onPointer = (e: MouseEvent) => {
            const t = e.target as Node;
            if (panelRef.current?.contains(t) || buttonRef.current?.contains(t)) return;
            setAnchor(null);
        };
        const onKey = (e: KeyboardEvent) => {
            if (e.key !== 'Escape') return;
            e.preventDefault();
            setAnchor(null);
            buttonRef.current?.focus();
        };
        // The panel is placed in fixed coordinates; a resize would strand it.
        const onResize = () => setAnchor(null);
        document.addEventListener('mousedown', onPointer);
        document.addEventListener('keydown', onKey);
        window.addEventListener('resize', onResize);
        return () => {
            document.removeEventListener('mousedown', onPointer);
            document.removeEventListener('keydown', onKey);
            window.removeEventListener('resize', onResize);
        };
    }, [open]);

    const openRow = (n: AppNotification) => {
        void markRead(n.id);
        close(false);
        if (n.link) navigate(n.link);
    };

    const panel = anchor
        ? createPortal(
              <div
                  ref={panelRef}
                  id={panelId}
                  role="dialog"
                  aria-modal="false"
                  aria-labelledby={headingId}
                  tabIndex={-1}
                  onBlur={e => {
                      // Tabbing out of the panel closes it (a pointer click
                      // outside is handled above).
                      const next = e.relatedTarget as Node | null;
                      if (next && !e.currentTarget.contains(next) && next !== buttonRef.current) close(false);
                  }}
                  style={{ left: anchor.left, bottom: anchor.bottom, width: PANEL_WIDTH, maxHeight: anchor.maxHeight }}
                  className="fixed z-[120] flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white text-slate-900 shadow-2xl shadow-slate-900/20 outline-none max-w-[calc(100vw-1rem)]"
              >
                  <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
                      <h2 id={headingId} className="text-sm font-bold text-slate-900">
                          Notifications
                          {count > 0 && <span className="ml-1.5 font-semibold text-slate-400">{count}</span>}
                      </h2>
                      <button
                          type="button"
                          onClick={() => void markAllRead(unread.map(n => n.id))}
                          disabled={count === 0}
                          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-semibold text-brand transition-colors hover:bg-brand/10 disabled:cursor-default disabled:text-slate-300 disabled:hover:bg-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                      >
                          <CheckCheck size={14} aria-hidden="true" />
                          Mark all as read
                      </button>
                  </div>
                  {count === 0 ? (
                      <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
                          <CheckCircle2 size={28} className="text-slate-300" aria-hidden="true" />
                          <p className="text-sm font-semibold text-slate-600">You're all caught up.</p>
                          <p className="text-xs text-slate-400">
                              Cubie lets you know here when a meeting prep or a scheduled task is ready.
                          </p>
                      </div>
                  ) : (
                      <ul className="min-h-0 flex-1 overflow-y-auto py-1">
                          {unread.map(n => {
                              const { Icon, tone } = rowIcon(n);
                              const when = relativeTime(n.created_at);
                              return (
                                  <li key={n.id}>
                                      <button
                                          type="button"
                                          onClick={() => openRow(n)}
                                          className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-slate-50 focus:bg-slate-50 focus:outline-none"
                                      >
                                          <span
                                              className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${tone}`}
                                          >
                                              <Icon size={15} aria-hidden="true" />
                                          </span>
                                          <span className="min-w-0 flex-1">
                                              <span className="block text-[13px] font-semibold leading-snug text-slate-900">
                                                  {n.title}
                                              </span>
                                              {n.body && (
                                                  <span className="mt-0.5 block text-xs leading-snug text-slate-600 line-clamp-2">
                                                      {n.body}
                                                  </span>
                                              )}
                                              {when && (
                                                  <span className="mt-1 block text-[11px] font-medium text-slate-400">
                                                      {when}
                                                  </span>
                                              )}
                                          </span>
                                      </button>
                                  </li>
                              );
                          })}
                      </ul>
                  )}
              </div>,
              document.body,
          )
        : null;

    return (
        <>
            <button
                ref={buttonRef}
                type="button"
                onClick={toggle}
                title={label}
                aria-label={label}
                aria-haspopup="dialog"
                aria-expanded={open}
                aria-controls={open ? panelId : undefined}
                className={`w-full flex items-center transition-colors hover:bg-white/5 focus:outline-none focus-visible:bg-white/10 ${
                    open ? 'bg-white/10 text-white' : 'text-gray-200 hover:text-white'
                } ${expanded ? 'gap-2.5 px-4 py-2.5' : 'justify-center py-2.5'}`}
            >
                <span className="relative flex-shrink-0">
                    <Bell size={expanded ? 17 : 19} aria-hidden="true" />
                    {!expanded && badge && (
                        <span
                            className="absolute -top-1.5 -right-2.5 min-w-[18px] h-[18px] px-1 rounded-full bg-brand-soft text-brand-hover text-[10px] font-bold leading-[18px] text-center ring-2 ring-rail"
                            aria-hidden="true"
                        >
                            {badge}
                        </span>
                    )}
                </span>
                {expanded && (
                    <>
                        <span className="text-sm truncate">Notifications</span>
                        {badge && (
                            <span
                                className="ml-auto min-w-[20px] h-5 px-1.5 rounded-full bg-brand-soft text-brand-hover text-[11px] font-bold leading-5 text-center"
                                aria-hidden="true"
                            >
                                {badge}
                            </span>
                        )}
                    </>
                )}
            </button>
            {panel}
        </>
    );
};

export default NotificationBell;
