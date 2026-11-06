import React, { useMemo, useState } from 'react';
import { Building2, CalendarRange, Check, ChevronsUpDown, Search, X } from 'lucide-react';
import type { InsightsDepartment } from '../../api/insights';
import {
    RANGE_LABELS,
    RANGE_PRESETS,
    RANGE_SHORT,
    customRangeError,
    filtersKey,
    toggleDepartment,
    withCustomDates,
    withDepartments,
    withRange,
} from '../../pages/insights/insightsFilters';
import type { InsightsFilters, RangePreset } from '../../pages/insights/insightsFilters';
import { usePopover } from './usePopover';

const CONTROL =
    'inline-flex h-9 items-center gap-2 rounded-md border border-gray-300 bg-white px-3 text-sm text-gray-700 shadow-sm transition-colors hover:border-gray-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30';

/**
 * The one row of filters above every dashboard: departments and a date range.
 * It scopes everything below it — every tile, chart and table re-reads the
 * same slice, so the numbers on a page always agree.
 *
 * It owns no filter state: it reads `filters` (parsed from the URL by the
 * page) and reports a new value through `onChange`, which writes the URL.
 */
export const FilterBar: React.FC<{
    filters: InsightsFilters;
    onChange: (next: InsightsFilters) => void;
    /** Already limited to what this viewer may see. */
    departments: readonly InsightsDepartment[];
    /** "vs previous 30 days". */
    comparison: string;
    /** "Sep 9 – Oct 8, 2026" — the period the numbers actually cover. */
    periodCaption?: string | null;
    /** Today in the company's business time zone, for a fresh custom range. */
    today: string;
    /** The department control is meaningless on a person's profile. */
    showDepartments?: boolean;
}> = ({ filters, onChange, departments, comparison, periodCaption, today, showDepartments = true }) => (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {showDepartments && <DepartmentPicker filters={filters} onChange={onChange} departments={departments} />}
        <RangeControl filters={filters} onChange={onChange} today={today} />
        <p className="text-xs text-gray-500">
            {periodCaption && <span className="font-medium text-gray-600 tabular-nums">{periodCaption}</span>}
            {periodCaption && ' · '}
            {comparison}
        </p>
        {showDepartments && filters.dept.length > 0 && (
            <ul className="flex w-full flex-wrap items-center gap-1.5" aria-label="Departments in the filter">
                {filters.dept.map(slug => {
                    const label = departments.find(d => d.slug === slug)?.label ?? slug;
                    return (
                        <li key={slug}>
                            <button
                                type="button"
                                onClick={() => onChange(toggleDepartment(filters, slug))}
                                className="inline-flex items-center gap-1 rounded-full bg-brand-tint px-2.5 py-1 text-xs font-medium text-brand transition-colors hover:bg-brand hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                                aria-label={`Remove ${label} from the filter`}
                            >
                                {label}
                                <X size={12} aria-hidden="true" />
                            </button>
                        </li>
                    );
                })}
                <li>
                    <button
                        type="button"
                        onClick={() => onChange(withDepartments(filters, []))}
                        className="rounded px-1.5 text-xs font-medium text-gray-500 hover:text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                    >
                        Clear
                    </button>
                </li>
            </ul>
        )}
    </div>
);

// ─── departments ─────────────────────────────────────────────────────────────

const DepartmentPicker: React.FC<{
    filters: InsightsFilters;
    onChange: (next: InsightsFilters) => void;
    departments: readonly InsightsDepartment[];
}> = ({ filters, onChange, departments }) => {
    const [draft, setDraft] = useState<string[]>(filters.dept);
    const [query, setQuery] = useState('');
    const { open, toggle, close, rootRef, triggerRef, panelId } = usePopover();

    if (departments.length === 0) return null;
    // A viewer who can see one department has nothing to pick.
    if (departments.length === 1) {
        return (
            <span className="inline-flex h-9 items-center gap-2 rounded-md border border-gray-200 bg-white/70 px-3 text-sm text-gray-600">
                <Building2 size={15} aria-hidden="true" className="text-gray-400" />
                {departments[0].label}
            </span>
        );
    }

    const selected = filters.dept;
    const triggerText =
        selected.length === 0
            ? 'All departments'
            : selected.length <= 2
              ? selected.map(s => departments.find(d => d.slug === s)?.label ?? s).join(', ')
              : `${selected.length} departments`;

    const openPicker = () => {
        if (!open) {
            setDraft(filters.dept);
            setQuery('');
        }
        toggle();
    };
    const apply = () => {
        // Every department ticked is the same as no filter: say "All".
        const all = draft.length === departments.length;
        onChange(withDepartments(filters, all ? [] : draft));
        close();
        triggerRef.current?.focus();
    };

    const q = query.trim().toLowerCase();
    const shown = q ? departments.filter(d => d.label.toLowerCase().includes(q) || d.slug.includes(q)) : departments;
    const draftIsAll = draft.length === 0 || draft.length === departments.length;

    return (
        <div ref={rootRef} className="relative">
            <button
                ref={triggerRef}
                type="button"
                onClick={openPicker}
                aria-expanded={open}
                aria-controls={panelId}
                aria-haspopup="dialog"
                className={`${CONTROL} max-w-[18rem]`}
            >
                <Building2 size={15} aria-hidden="true" className="flex-shrink-0 text-gray-400" />
                <span className="sr-only">Departments: </span>
                <span className="truncate">{triggerText}</span>
                <ChevronsUpDown size={14} aria-hidden="true" className="flex-shrink-0 text-gray-400" />
            </button>
            {open && (
                <div
                    id={panelId}
                    role="dialog"
                    aria-label="Choose departments"
                    className="absolute left-0 top-full z-30 mt-2 w-72 max-w-[85vw] rounded-lg border border-gray-200 bg-white shadow-lg"
                >
                    {departments.length > 6 && (
                        <div className="border-b border-gray-100 p-2">
                            <label className="relative block">
                                <span className="sr-only">Search departments</span>
                                <Search size={14} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
                                <input
                                    type="search"
                                    value={query}
                                    onChange={e => setQuery(e.target.value)}
                                    placeholder="Search departments…"
                                    autoFocus
                                    className="w-full rounded-md border border-gray-300 py-1.5 pl-8 pr-2 text-sm focus:border-brand/50 focus:outline-none focus:ring-2 focus:ring-brand/30"
                                />
                            </label>
                        </div>
                    )}
                    <div className="flex items-center justify-between px-3 pb-1 pt-2 text-xs">
                        <button
                            type="button"
                            onClick={() => setDraft(departments.map(d => d.slug))}
                            className="rounded font-medium text-brand hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                        >
                            Select all
                        </button>
                        <button
                            type="button"
                            onClick={() => setDraft([])}
                            className="rounded font-medium text-gray-500 hover:text-gray-800 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                        >
                            Select none
                        </button>
                    </div>
                    <fieldset className="max-h-64 overflow-y-auto px-1 pb-1">
                        <legend className="sr-only">Departments</legend>
                        {shown.length === 0 && <p className="px-2 py-3 text-sm text-gray-500">No department matches “{query}”.</p>}
                        {shown.map(d => {
                            const checked = draft.includes(d.slug);
                            return (
                                <label
                                    key={d.slug}
                                    className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-gray-700 hover:bg-gray-50"
                                >
                                    <input
                                        type="checkbox"
                                        checked={checked}
                                        onChange={() =>
                                            setDraft(prev => (prev.includes(d.slug) ? prev.filter(s => s !== d.slug) : [...prev, d.slug]))
                                        }
                                        className="h-4 w-4 rounded border-gray-300 text-brand accent-brand focus:ring-brand/30"
                                    />
                                    <span className="flex-1 truncate">{d.label}</span>
                                    <span className="text-xs text-gray-400 tabular-nums" title={`${d.people} people with Cubie`}>
                                        {d.people}
                                    </span>
                                </label>
                            );
                        })}
                    </fieldset>
                    <div className="flex items-center justify-between gap-2 border-t border-gray-100 px-3 py-2">
                        <span className="text-xs text-gray-500">
                            {draftIsAll ? 'All departments' : `${draft.length} of ${departments.length} selected`}
                        </span>
                        <div className="flex gap-1.5">
                            <button
                                type="button"
                                onClick={() => {
                                    close();
                                    triggerRef.current?.focus();
                                }}
                                className="rounded-md px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/30"
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                onClick={apply}
                                className="rounded-md bg-brand px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-1"
                            >
                                Apply
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

// ─── date range ──────────────────────────────────────────────────────────────

const RangeControl: React.FC<{
    filters: InsightsFilters;
    onChange: (next: InsightsFilters) => void;
    today: string;
}> = ({ filters, onChange, today }) => (
    <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-label="Date range" className="inline-flex h-9 overflow-hidden rounded-md border border-gray-300 bg-white shadow-sm">
            {RANGE_PRESETS.map((r: RangePreset, i) => {
                const on = filters.range === r;
                return (
                    <button
                        key={r}
                        type="button"
                        aria-pressed={on}
                        title={RANGE_LABELS[r]}
                        onClick={() => onChange(withRange(filters, r, today))}
                        className={`px-2.5 text-xs font-semibold transition-colors focus:outline-none focus-visible:relative focus-visible:z-[1] focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand/40 sm:px-3 ${
                            i > 0 ? 'border-l border-gray-200' : ''
                        } ${on ? 'bg-brand text-white' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'}`}
                    >
                        <span aria-hidden="true">{RANGE_SHORT[r]}</span>
                        <span className="sr-only">{RANGE_LABELS[r]}</span>
                    </button>
                );
            })}
        </div>
        {filters.range === 'custom' && (
            // Keyed on the applied range, so the inputs reset whenever the URL changes under them.
            <CustomDates key={filtersKey(filters)} filters={filters} onChange={onChange} today={today} />
        )}
    </div>
);

const CustomDates: React.FC<{
    filters: InsightsFilters;
    onChange: (next: InsightsFilters) => void;
    today: string;
}> = ({ filters, onChange, today }) => {
    const [start, setStart] = useState(filters.start ?? '');
    const [end, setEnd] = useState(filters.end ?? '');
    const error = useMemo(() => customRangeError(start, end), [start, end]);
    const dirty = start !== filters.start || end !== filters.end;

    const commit = (s: string, e: string) => {
        const next = withCustomDates(filters, s, e);
        if (next) onChange(next);
    };
    // Enter applies; nothing applies on blur, because applying rewrites the URL
    // and would pull focus out of the second date while you tab into it.
    const onKeyDown = (ev: React.KeyboardEvent<HTMLInputElement>) => {
        if (ev.key === 'Enter' && !error && dirty) {
            ev.preventDefault();
            commit(start, end);
        }
    };

    return (
        <div className="flex flex-wrap items-center gap-1.5">
            <CalendarRange size={15} aria-hidden="true" className="text-gray-400" />
            <label className="sr-only" htmlFor="insights-start">
                Start date
            </label>
            <input
                id="insights-start"
                type="date"
                value={start}
                max={end || today}
                onChange={e => setStart(e.target.value)}
                onKeyDown={onKeyDown}
                className="h-9 rounded-md border border-gray-300 bg-white px-2 text-sm text-gray-700 focus:border-brand/50 focus:outline-none focus:ring-2 focus:ring-brand/30"
            />
            <span className="text-xs text-gray-400">to</span>
            <label className="sr-only" htmlFor="insights-end">
                End date
            </label>
            <input
                id="insights-end"
                type="date"
                value={end}
                min={start || undefined}
                max={today}
                onChange={e => setEnd(e.target.value)}
                onKeyDown={onKeyDown}
                className="h-9 rounded-md border border-gray-300 bg-white px-2 text-sm text-gray-700 focus:border-brand/50 focus:outline-none focus:ring-2 focus:ring-brand/30"
            />
            {dirty && !error && (
                <button
                    type="button"
                    onClick={() => commit(start, end)}
                    className="inline-flex h-9 items-center gap-1 rounded-md bg-brand px-3 text-xs font-medium text-white hover:bg-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-1"
                >
                    <Check size={13} aria-hidden="true" />
                    Apply
                </button>
            )}
            {error && (start || end) && (
                <p role="alert" className="w-full text-xs text-red-700 sm:w-auto">
                    {error}
                </p>
            )}
        </div>
    );
};
