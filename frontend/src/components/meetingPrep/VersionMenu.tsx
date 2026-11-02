// The version picker in the Meeting Prep header: "v3 of 5 ▾" / "v5 · latest".
//
// A button that opens a listbox of every version, newest first, each saying
// what changed and when ("v5 · You edited Talking Points · Oct 1, 2:14 PM").
// Picking one shows it; older versions are read-only on the page, with a
// Restore action there rather than here, so opening a version can never
// change anything by itself.
//
// Keyboard: Enter / Space / ArrowDown on the button opens the list; ArrowUp /
// ArrowDown / Home / End move; Enter or Space picks; Esc closes and returns
// focus to the button; Tab closes.
import React, { useEffect, useId, useRef, useState } from 'react';
import { Check, ChevronDown, History, Loader2 } from 'lucide-react';

import type { VersionSummary } from '../../api/meetingPrep';
import { describeChange, formatVersionWhen, versionMenuLabel } from '../../pages/meetingPrep/sections';
import { cn } from './cn';

export interface VersionMenuProps {
    /** Newest first. */
    versions: VersionSummary[];
    currentRunId: string | null;
    currentVersion: number | null;
    latestVersion: number | null;
    isInternal: boolean;
    /** A version is being opened. */
    busy?: boolean;
    disabled?: boolean;
    onSelect: (version: VersionSummary) => void;
}

export const VersionMenu: React.FC<VersionMenuProps> = ({
    versions,
    currentRunId,
    currentVersion,
    latestVersion,
    isInternal,
    busy = false,
    disabled = false,
    onSelect,
}) => {
    const [open, setOpen] = useState(false);
    const [active, setActive] = useState(0);
    const rootRef = useRef<HTMLDivElement>(null);
    const buttonRef = useRef<HTMLButtonElement>(null);
    const listRef = useRef<HTMLUListElement>(null);
    const baseId = useId();
    const optionId = (i: number) => `${baseId}-opt-${i}`;

    // Focus the list when it opens, so the arrow keys work straight away.
    useEffect(() => {
        if (open) listRef.current?.focus();
    }, [open]);

    // Keep the highlighted option in view in a long history.
    useEffect(() => {
        if (open) document.getElementById(`${baseId}-opt-${active}`)?.scrollIntoView({ block: 'nearest' });
    }, [open, active, baseId]);

    // A click anywhere else closes it.
    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', onDown);
        return () => document.removeEventListener('mousedown', onDown);
    }, [open]);

    if (!currentVersion) return null;

    const currentIndex = Math.max(
        0,
        versions.findIndex(v => v.run_id === currentRunId),
    );

    const openMenu = (at?: number) => {
        if (disabled || !versions.length) return;
        setActive(at ?? currentIndex);
        setOpen(true);
    };

    const closeMenu = (refocus: boolean) => {
        setOpen(false);
        if (refocus) buttonRef.current?.focus();
    };

    const pick = (i: number) => {
        const v = versions[i];
        closeMenu(true);
        if (v && v.run_id !== currentRunId) onSelect(v);
    };

    const onListKey = (e: React.KeyboardEvent<HTMLUListElement>) => {
        const last = versions.length - 1;
        switch (e.key) {
            case 'ArrowDown':
                e.preventDefault();
                setActive(a => Math.min(last, a + 1));
                break;
            case 'ArrowUp':
                e.preventDefault();
                setActive(a => Math.max(0, a - 1));
                break;
            case 'Home':
                e.preventDefault();
                setActive(0);
                break;
            case 'End':
                e.preventDefault();
                setActive(last);
                break;
            case 'Enter':
            case ' ':
                e.preventDefault();
                pick(active);
                break;
            case 'Escape':
                e.preventDefault();
                e.stopPropagation();
                closeMenu(true);
                break;
            case 'Tab':
                closeMenu(false);
                break;
        }
    };

    return (
        <div ref={rootRef} data-tour="meeting-prep-versions" className="relative no-print">
            <button
                ref={buttonRef}
                type="button"
                aria-haspopup="listbox"
                aria-expanded={open}
                aria-label={`Version ${currentVersion}${latestVersion ? ` of ${latestVersion}` : ''}. Show version history`}
                disabled={disabled}
                onClick={() => (open ? closeMenu(false) : openMenu())}
                onKeyDown={e => {
                    if (e.key === 'ArrowDown' && !open) {
                        e.preventDefault();
                        openMenu();
                    }
                }}
                className={cn(
                    'inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-bold shadow-sm transition-colors focus:outline-none focus-visible:ring-4 focus-visible:ring-brand/15 disabled:cursor-not-allowed disabled:opacity-60',
                    open
                        ? 'border-brand/40 bg-brand/5 text-brand'
                        : 'border-slate-200/70 bg-white text-slate-700 hover:border-brand/30 hover:text-brand',
                )}
            >
                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <History className="h-3.5 w-3.5" />}
                {versionMenuLabel(currentVersion, latestVersion)}
                <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-180')} />
            </button>

            {open && (
                <ul
                    ref={listRef}
                    role="listbox"
                    tabIndex={-1}
                    aria-label="Versions of this briefing, newest first"
                    aria-activedescendant={optionId(active)}
                    onKeyDown={onListKey}
                    className="absolute left-0 top-full z-30 mt-2 max-h-80 w-[min(24rem,calc(100vw-3rem))] overflow-y-auto rounded-2xl border border-slate-200 bg-white p-1.5 shadow-2xl shadow-slate-300/40 outline-none"
                >
                    {versions.map((v, i) => {
                        const selected = v.run_id === currentRunId;
                        return (
                            <li
                                key={v.run_id}
                                id={optionId(i)}
                                role="option"
                                aria-selected={selected}
                                onMouseEnter={() => setActive(i)}
                                onClick={() => pick(i)}
                                className={cn(
                                    'flex cursor-pointer items-start gap-2.5 rounded-xl px-3 py-2 text-left',
                                    i === active ? 'bg-slate-100' : '',
                                )}
                            >
                                <span
                                    className={cn(
                                        'mt-0.5 shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-black tabular-nums',
                                        selected ? 'bg-brand text-white' : 'bg-slate-100 text-slate-600',
                                    )}
                                >
                                    v{v.version}
                                </span>
                                <span className="min-w-0 flex-1">
                                    <span className="block text-sm font-semibold leading-snug text-slate-800">
                                        {describeChange(v, isInternal)}
                                    </span>
                                    <span className="mt-0.5 flex items-center gap-1.5 text-[11px] text-slate-400">
                                        {formatVersionWhen(v.created_at)}
                                        {v.is_latest && (
                                            <span className="rounded-full bg-emerald-50 px-1.5 py-px font-bold text-emerald-700">
                                                Latest
                                            </span>
                                        )}
                                    </span>
                                </span>
                                {selected && <Check className="mt-1 h-4 w-4 shrink-0 text-brand" aria-hidden />}
                            </li>
                        );
                    })}
                </ul>
            )}
        </div>
    );
};
