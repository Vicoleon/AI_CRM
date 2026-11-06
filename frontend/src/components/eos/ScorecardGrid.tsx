import React, { useMemo, useState } from 'react';
import { AlertTriangle, GripVertical, Info, TrendingUp } from 'lucide-react';
import { Tooltip } from '../Tooltip';
import {
    formatPerson,
    formatScorecardGoal,
    formatScorecardValue,
    fromEditableScorecardValue,
    goalGap,
    goalMet,
    GOAL_GAP_LABEL,
    GOAL_GAP_MESSAGE,
    toEditableScorecardValue,
    weekKey,
} from '../../pages/eos/_utils';
import type { PersonLite, ScorecardValueType } from '../../pages/eos/_utils';

/**
 * The scorecard grid — one component, two hosts.
 *
 * The /eos/scorecard page and the in-meeting L10 panel used to be independent
 * implementations of the same table, sharing only the pure format helpers. The
 * exec team's UAT verdict was blunt: they read the scorecard backwards over a
 * quarter and wanted the page's layout inside the meeting. Rather than port the
 * markup a second time (and let the two drift again), the table lives here and
 * the hosts differ only in what they pass:
 *
 * - the page allows editing every visible week; the meeting allows only the
 *   current one (`canEditWeek`), so the "you log THIS week" rule survives
 *   inside the borrowed layout,
 * - the page owns 13 weeks of entries and computes its own rolling averages;
 *   the meeting takes the server's (`averagesFor`),
 * - only the page renders the per-row edit/delete actions (`rowActions`).
 *
 * Frozen left columns: Owner / Measurable / Goal stay pinned
 * while the weekly columns scroll horizontally, and the header row stays
 * pinned while the rows scroll vertically.
 *
 * Frozen RIGHT columns (UAT feedback): "the user has to scroll to the far
 * right to see the 4W avg and 13W avg". Thirteen weekly columns are wider than
 * any screen, so the two numbers the meeting actually reasons about — the
 * rolling averages — lived past the right edge, and reading one meant scrolling
 * away from the measurable's name. They now pin to the right edge exactly as
 * Owner / Measurable / Goal pin to the left, so every row reads
 * "who · what · goal · …weeks… · 4w · 13w" without moving the scrollbar at all.
 * The per-row action cell pins with them (it sits outboard of the averages, so
 * leaving it loose would let it slide underneath them).
 *
 * This requires table-layout:fixed + a <colgroup> so the widths below are
 * honoured EXACTLY. (A first attempt used table-layout:auto and hardcoded the
 * sticky left-offsets against the intended widths — but an auto-layout table
 * that overflows horizontally ignores width hints and renders the columns
 * narrower, so the Goal column detached from Measurable and the scrolling
 * weekly cells bled through the gap.) All widths are in rem so the sticky
 * left- and right-offsets derive from the same numbers and never drift.
 */
const COL_W = { owner: 11, measurable: 14, goal: 10, week: 6, avg: 7, actions: 6 };
// Running left-offset (rem) for each frozen column = sum of widths to its left.
const LEFT = {
    owner: 0,
    measurable: COL_W.owner,
    goal: COL_W.owner + COL_W.measurable,
};
// Frozen COLUMN cells sit above the scrolling weekly cells; callers append the
// per-row background (bg-gray-50 in the header, bg-white/group-hover in the
// body) so nothing shows through as columns slide underneath, and the Goal
// column carries a right border as the divider from the scrolling area.
const FROZEN = 'sticky z-20';
// The header row pins vertically. Its three frozen cells pin on BOTH axes and
// need to outrank everything, or the corner gets overrun by whichever
// single-axis cell slides under it.
const FROZEN_HEADER = 'sticky top-0 z-30';
// Sells the right-hand pin: weekly cells slide UNDER the averages, and without
// an edge they appear to be overwritten rather than covered.
const RIGHT_EDGE = 'shadow-[-6px_0_8px_-6px_rgba(15,23,42,0.18)]';

export interface ScorecardGridMetric {
    id: string;
    name: string;
    description: string | null;
    goal_description: string | null;
    target_value: string | null;
    value_type: ScorecardValueType | null;
    comparator: string | null;
    owner_id: string | null;
    /** Display position within the owner's block; null = alphabetical fallback. */
    sort_order?: number | null;
}

interface CellProps {
    metric: ScorecardGridMetric;
    rawValue: string;
    note: string | null;
    editable: boolean;
    onSave: (value: string) => void;
    onSaveNote: ((note: string) => void) | null;
}

/**
 * One weekly cell. Reads as a formatted, goal-coloured value (e.g. "$34,300" in
 * green); click turns it into a text input. Storage stays canonical — percent as
 * a fraction (0.9495), currency/number as a plain number — and the display layer
 * formats it per the metric's value_type.
 *
 * The input edits in the SAME units the cell displays, so a percent cell is
 * typed in percent points ("50" is 50%, stored as 0.5) and a percent cell you
 * open shows what it displays ("9.25", not "0.0925"). The `%` affix says so.
 *
 * A cell can also carry a NOTE — what the number is made of ("3 renewals, 2
 * upsells"), the spreadsheet cell comment the exec team lost in the move. It
 * rides in the same edit panel rather than behind a second affordance, and the
 * panel commits on Enter or when focus leaves it entirely, so the hot path
 * (click, type a number, Enter) is unchanged from the single-input version.
 */
const ScorecardCell: React.FC<CellProps> = ({
    metric,
    rawValue,
    note,
    editable,
    onSave,
    onSaveNote,
}) => {
    const [editing, setEditing] = useState(false);
    const [draftValue, setDraftValue] = useState('');
    const [draftNote, setDraftNote] = useState('');
    const met = goalMet(rawValue, metric.comparator, metric.target_value);
    const display = formatScorecardValue(rawValue, metric.value_type);
    const isPercent = metric.value_type === 'percent';

    const open = () => {
        if (!editable) return;
        setDraftValue(toEditableScorecardValue(rawValue, metric.value_type));
        setDraftNote(note ?? '');
        setEditing(true);
    };

    const commit = () => {
        setEditing(false);
        const nextValue = fromEditableScorecardValue(draftValue, metric.value_type);
        if (nextValue !== rawValue) onSave(nextValue);
        if (onSaveNote && draftNote.trim() !== (note ?? '').trim()) {
            onSaveNote(draftNote.trim());
        }
    };

    if (editing) {
        return (
            <div
                className="relative inline-block"
                // Commit when focus leaves the PANEL, not the input — tabbing
                // from the value to the note must not close and save.
                onBlur={e => {
                    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
                        commit();
                    }
                }}
            >
                <span className="relative inline-block w-20">
                    <input
                        type="text"
                        autoFocus
                        value={draftValue}
                        onChange={e => setDraftValue(e.target.value)}
                        onKeyDown={e => {
                            if (e.key === 'Enter') commit();
                            if (e.key === 'Escape') setEditing(false);
                        }}
                        className={`w-full text-center py-1 text-sm rounded border border-brand focus:ring-1 focus:ring-brand outline-none ${
                            isPercent ? 'pl-1 pr-4' : 'px-1'
                        }`}
                    />
                    {isPercent && (
                        <span className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 text-xs text-gray-400">
                            %
                        </span>
                    )}
                </span>
                {onSaveNote && (
                    /* Sized by its content (`w-max`) rather than fixed, so the
                       textarea's resize handle actually grows the panel with
                       it instead of overflowing a 13rem box. A note that
                       explains a number ("3 renewals, 2 upsells, 1 carried
                       over from June") outgrows two rows fast, and it's read
                       here as often as it's typed. */
                    <div className="absolute left-0 top-full mt-1 z-40 w-max bg-white border border-brand rounded-md shadow-lg p-2 text-left">
                        <label className="block text-[10px] uppercase tracking-wider font-semibold text-gray-500 mb-1">
                            Note
                        </label>
                        <textarea
                            value={draftNote}
                            onChange={e => setDraftNote(e.target.value)}
                            onKeyDown={e => {
                                if (e.key === 'Escape') setEditing(false);
                            }}
                            rows={2}
                            placeholder="What's this number made of?"
                            className="block w-52 min-w-[13rem] max-w-[32rem] min-h-[2.75rem] max-h-[18rem] resize border border-gray-200 rounded px-2 py-1 text-xs focus:ring-1 focus:ring-brand outline-none"
                        />
                        <p className="mt-1 w-52 text-[10px] text-gray-400">
                            Drag the corner to resize. Enter in the value saves both; Esc
                            discards.
                        </p>
                    </div>
                )}
            </div>
        );
    }

    const colour =
        met === true
            ? 'text-green-700 bg-green-50'
            : met === false
              ? 'text-red-700 bg-red-50'
              : 'text-gray-700';

    const body = (
        <span
            className={`relative inline-block w-20 py-1 px-1 text-sm rounded font-mono tabular-nums transition ${colour} ${
                editable ? 'hover:ring-1 hover:ring-gray-300' : ''
            }`}
        >
            {display || <span className="text-gray-300">—</span>}
            {note && (
                <span
                    className="absolute top-0 right-0.5 text-brand leading-none"
                    aria-hidden="true"
                >
                    •
                </span>
            )}
        </span>
    );

    // A read-only cell gets the rich Tooltip; an editable one can't, because
    // Tooltip renders a focusable span and nesting that inside the cell's
    // <button> is invalid interactive-content nesting (and a second tab stop
    // per cell — 13 columns of them). The editable cell puts the note on its
    // own native `title` instead.
    if (!editable) {
        if (note) {
            return <Tooltip content={note}>{body}</Tooltip>;
        }
        return (
            <span
                className="inline-block"
                title="Logged in an earlier week — edit it on the Scorecard page"
            >
                {body}
            </span>
        );
    }

    const editHint = isPercent
        ? 'Click to edit — type a whole percent, e.g. 50 for 50%'
        : 'Click to edit';

    return (
        <button
            type="button"
            onClick={open}
            title={note ? `${note}\n\n${editHint}` : editHint}
        >
            {body}
        </button>
    );
};

export interface ScorecardGridPropsLabeling {
    /**
     * Days added to each week-start date for the HEADER LABEL only (cell keys
     * and storage stay on the week's Sunday). A biweekly team whose L10 meets
     * on Thursdays reads its columns as the meeting dates ("Sep 17"), not the
     * internal week start ("Sep 13"). 0 (default) labels the Sunday itself.
     */
    labelOffsetDays?: number;
}

export interface ScorecardGridProps extends ScorecardGridPropsLabeling {
    metrics: ScorecardGridMetric[];
    /** The week axis, newest → oldest. */
    weeks: Date[];
    peopleById: Map<string, PersonLite>;
    /** Raw stored value for a cell — '' when the week has no entry. */
    valueAt: (metricId: string, week: string) => string;
    /** Cell note, when the host carries them. */
    noteAt?: (metricId: string, week: string) => string | null;
    /**
     * Rolling averages for a row. The host computes these because it owns the
     * wider history: the page walks 13 weeks of entries even when only 4 are
     * visible, and the meeting takes the server's precomputed pair.
     */
    averagesFor: (metricId: string) => { avg4: number | null; avg13: number | null };
    /** Defaults to every week editable (the page). */
    canEditWeek?: (week: string) => boolean;
    onSaveCell: (metricId: string, weekDate: Date, value: string) => void;
    onSaveNote?: (metricId: string, weekDate: Date, note: string) => void;
    /** Trailing per-row action cell (the page's edit/delete). */
    rowActions?: (metric: ScorecardGridMetric) => React.ReactNode;
    /**
     * When provided, an owner's block becomes drag-reorderable (a grip appears
     * on hover) and this is called with that owner's measurable ids in their new
     * order. Passed only by the Scorecard page; the L10 grid omits it, so the
     * meeting view stays read-only for ordering.
     */
    onReorder?: (orderedIds: string[]) => void;
    /**
     * Bounds the scrollport so the header can stick. `overflow-x-auto` alone
     * makes the other axis compute to `auto` with a scrollport equal to the
     * content height — i.e. nothing to stick against.
     */
    maxHeightClass?: string;
}

export const ScorecardGrid: React.FC<ScorecardGridProps> = ({
    metrics,
    weeks,
    labelOffsetDays = 0,
    peopleById,
    valueAt,
    noteAt,
    averagesFor,
    canEditWeek,
    onSaveCell,
    onSaveNote,
    rowActions,
    onReorder,
    maxHeightClass = 'max-h-[70vh]',
}) => {
    // Group rows by owner so the table reads like the source spreadsheet —
    // each leader's measurables clustered, owner name shown once per block.
    // Within a block the team's drag order (`sort_order`) wins; measurables
    // never reordered fall back to alphabetical (nulls last). Mirrors the page's
    // own sort so the two agree, and lets the L10 grid honour the order too.
    const orderedMetrics = useMemo(() => {
        const nameOf = (m: ScorecardGridMetric) =>
            formatPerson(m.owner_id ? peopleById.get(m.owner_id) : undefined, '~');
        const posOf = (m: ScorecardGridMetric) =>
            m.sort_order == null ? Number.MAX_SAFE_INTEGER : m.sort_order;
        return [...metrics].sort(
            (a, b) =>
                nameOf(a).localeCompare(nameOf(b)) ||
                posOf(a) - posOf(b) ||
                a.name.localeCompare(b.name),
        );
    }, [metrics, peopleById]);

    // Drag-to-reorder (Scorecard page only, when `onReorder` is provided).
    // Native HTML5 DnD — no library. `dragId` is the row being dragged, `overId`
    // the row it's hovering. Moves are confined to one owner's block.
    const [dragId, setDragId] = useState<string | null>(null);
    const [overId, setOverId] = useState<string | null>(null);
    const ownerKey = (m: ScorecardGridMetric) => m.owner_id ?? '';

    // How many measurables each owner has — a grip only makes sense on a block
    // with more than one row.
    const ownerCount = useMemo(() => {
        const c = new Map<string, number>();
        for (const m of orderedMetrics) {
            const k = ownerKey(m);
            c.set(k, (c.get(k) ?? 0) + 1);
        }
        return c;
    }, [orderedMetrics]);

    const dropOnto = (targetId: string) => {
        const dragged = orderedMetrics.find(x => x.id === dragId);
        const target = orderedMetrics.find(x => x.id === targetId);
        setDragId(null);
        setOverId(null);
        if (
            !onReorder ||
            !dragged ||
            !target ||
            dragged.id === target.id ||
            ownerKey(dragged) !== ownerKey(target) // never move across owners
        ) {
            return;
        }
        // Rebuild the owner's id list with `dragged` moved to just before
        // `target`, then hand the whole block's new order to the page.
        const ids = orderedMetrics
            .filter(x => ownerKey(x) === ownerKey(dragged))
            .map(x => x.id)
            .filter(id => id !== dragged.id);
        ids.splice(ids.indexOf(target.id), 0, dragged.id);
        onReorder(ids);
    };

    const actionsWidth = rowActions ? COL_W.actions : 0;
    const totalWidth =
        COL_W.owner +
        COL_W.measurable +
        COL_W.goal +
        weeks.length * COL_W.week +
        COL_W.avg * 2 +
        actionsWidth;

    // Running right-offset (rem) for each pinned column = sum of widths to its
    // right. Derived from `actionsWidth` rather than hardcoded, so the meeting
    // (no action column) pins the averages flush to the edge and the page (with
    // one) leaves exactly its width beside them.
    const RIGHT = {
        actions: 0,
        avg13: actionsWidth,
        avg4: actionsWidth + COL_W.avg,
    };

    return (
        <div
            className={`bg-white rounded-lg border border-gray-200 shadow-sm overflow-auto ${maxHeightClass}`}
            data-tour="eos-scorecard-grid"
        >
            <table className="table-fixed text-sm" style={{ width: `${totalWidth}rem` }}>
                <colgroup>
                    <col style={{ width: `${COL_W.owner}rem` }} />
                    <col style={{ width: `${COL_W.measurable}rem` }} />
                    <col style={{ width: `${COL_W.goal}rem` }} />
                    {weeks.map((_, i) => (
                        <col key={i} style={{ width: `${COL_W.week}rem` }} />
                    ))}
                    <col style={{ width: `${COL_W.avg}rem` }} />
                    <col style={{ width: `${COL_W.avg}rem` }} />
                    {rowActions && <col style={{ width: `${COL_W.actions}rem` }} />}
                </colgroup>
                <thead className="bg-gray-50 border-b border-gray-200">
                    <tr>
                        <th
                            className={`${FROZEN_HEADER} bg-gray-50 px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider`}
                            style={{ left: `${LEFT.owner}rem` }}
                        >
                            Owner
                        </th>
                        <th
                            className={`${FROZEN_HEADER} bg-gray-50 px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider`}
                            style={{ left: `${LEFT.measurable}rem` }}
                            data-tour="eos-scorecard-measurables"
                        >
                            Measurable
                        </th>
                        <th
                            className={`${FROZEN_HEADER} bg-gray-50 px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider border-r border-gray-200`}
                            style={{ left: `${LEFT.goal}rem` }}
                        >
                            Goal
                        </th>
                        {weeks.map((w, i) => (
                            <th
                                key={i}
                                className="sticky top-0 z-10 bg-gray-50 px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wider"
                            >
                                {new Date(
                                    w.getFullYear(),
                                    w.getMonth(),
                                    w.getDate() + labelOffsetDays,
                                ).toLocaleDateString('en-US', {
                                    month: 'short',
                                    day: 'numeric',
                                })}
                            </th>
                        ))}
                        {/* Cumulative averages — the 4-week and 13-week
                            averages alongside the weekly cells so the team can
                            spot trend drift at a glance. Goal-coloured like any
                            other cell: they were the far-right columns rendering
                            flat grey, which is what "go all the way to the right
                            and that one should've colour-coded" was about. */}
                        <th
                            className={`${FROZEN_HEADER} ${RIGHT_EDGE} px-3 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-100 border-l border-gray-200`}
                            style={{ right: `${RIGHT.avg4}rem` }}
                            title="Average of the last 4 weeks where a numeric value was recorded"
                        >
                            <span className="inline-flex items-center gap-1">
                                <TrendingUp size={11} /> 4w avg
                            </span>
                        </th>
                        <th
                            className={`${FROZEN_HEADER} px-3 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wider bg-gray-100`}
                            style={{ right: `${RIGHT.avg13}rem` }}
                            title="Average of the last 13 weeks (a quarter) where a numeric value was recorded"
                        >
                            <span className="inline-flex items-center gap-1">
                                <TrendingUp size={11} /> 13w avg
                            </span>
                        </th>
                        {rowActions && (
                            <th
                                className={`${FROZEN_HEADER} bg-gray-100`}
                                style={{ right: `${RIGHT.actions}rem` }}
                            />
                        )}
                    </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                    {orderedMetrics.map((m, idx) => {
                        const { avg4, avg13 } = averagesFor(m.id);
                        const gap = goalGap(m);
                        const avg4Met = goalMet(
                            avg4 === null ? '' : String(avg4),
                            m.comparator,
                            m.target_value,
                        );
                        const avg13Met = goalMet(
                            avg13 === null ? '' : String(avg13),
                            m.comparator,
                            m.target_value,
                        );
                        const avgColour = (met: boolean | null) =>
                            met === true
                                ? 'text-green-700'
                                : met === false
                                  ? 'text-red-700'
                                  : 'text-gray-800';
                        // Owner shown once per block (spreadsheet-style grouping).
                        const ownerName = formatPerson(
                            m.owner_id ? peopleById.get(m.owner_id) : undefined,
                        );
                        const prevMetric = idx > 0 ? orderedMetrics[idx - 1] : null;
                        const prevOwner = prevMetric
                            ? formatPerson(
                                  prevMetric.owner_id
                                      ? peopleById.get(prevMetric.owner_id)
                                      : undefined,
                              )
                            : null;
                        const showOwner = ownerName !== prevOwner;
                        // A grip shows only where reordering is enabled and the
                        // owner has more than one measurable to sort.
                        const canDrag =
                            !!onReorder && (ownerCount.get(ownerKey(m)) ?? 0) > 1;
                        return (
                            <tr
                                key={m.id}
                                className={`hover:bg-gray-50 group ${
                                    dragId === m.id ? 'opacity-40' : ''
                                } ${
                                    overId === m.id && dragId && dragId !== m.id
                                        ? 'border-t-2 border-brand'
                                        : ''
                                }`}
                                onDragOver={
                                    onReorder
                                        ? e => {
                                              const dragged = orderedMetrics.find(
                                                  x => x.id === dragId,
                                              );
                                              if (
                                                  dragged &&
                                                  ownerKey(dragged) === ownerKey(m)
                                              ) {
                                                  e.preventDefault();
                                                  setOverId(m.id);
                                              }
                                          }
                                        : undefined
                                }
                                onDrop={
                                    onReorder
                                        ? e => {
                                              e.preventDefault();
                                              dropOnto(m.id);
                                          }
                                        : undefined
                                }
                            >
                                <td
                                    className={`${FROZEN} bg-white group-hover:bg-gray-50 px-4 py-3 text-gray-700 font-medium`}
                                    style={{ left: `${LEFT.owner}rem` }}
                                >
                                    {showOwner ? ownerName : ''}
                                </td>
                                <td
                                    className={`${FROZEN} bg-white group-hover:bg-gray-50 px-4 py-3 font-medium text-gray-900`}
                                    style={{ left: `${LEFT.measurable}rem` }}
                                >
                                    {/* The description is only ever readable
                                        here, so it hangs off a real tooltip
                                        rather than a native `title`. */}
                                    <div className="flex items-center gap-1.5">
                                        {canDrag && (
                                            <span
                                                draggable
                                                onDragStart={e => {
                                                    setDragId(m.id);
                                                    e.dataTransfer.effectAllowed = 'move';
                                                }}
                                                onDragEnd={() => {
                                                    setDragId(null);
                                                    setOverId(null);
                                                }}
                                                className="cursor-grab active:cursor-grabbing text-gray-300 hover:text-gray-500 opacity-0 group-hover:opacity-100 shrink-0"
                                                aria-label="Drag to reorder"
                                                title="Drag to reorder"
                                            >
                                                <GripVertical size={14} />
                                            </span>
                                        )}
                                        <Tooltip
                                            content={m.description}
                                            className="max-w-full gap-1 cursor-help"
                                        >
                                            <span className="min-w-0 break-words">
                                                {m.name}
                                            </span>
                                            {m.description ? (
                                                <Info
                                                    size={13}
                                                    className="shrink-0 text-gray-400"
                                                    aria-hidden="true"
                                                />
                                            ) : null}
                                        </Tooltip>
                                    </div>
                                </td>
                                <td
                                    className={`${FROZEN} bg-white group-hover:bg-gray-50 px-4 py-3 text-gray-500 border-r border-gray-200`}
                                    style={{ left: `${LEFT.goal}rem` }}
                                >
                                    {/* Formatted, not raw: a percent target fell
                                        out of storage here as "0.98" — the one
                                        place in the app a percent didn't read as
                                        a percent, and the only place a
                                        mis-scaled one could hide. */}
                                    <span className="flex items-center gap-1">
                                        {formatScorecardGoal(m) || '—'}
                                        {/* Flags EITHER missing half. Checking
                                            only the comparator meant a
                                            measurable with a target-less
                                            comparator showed its goal, coloured
                                            nothing, and warned about nothing —
                                            the whole Exec scorecard read as
                                            broken colouring. */}
                                        {gap && (
                                            <Tooltip content={GOAL_GAP_MESSAGE[gap]}>
                                                <AlertTriangle
                                                    size={12}
                                                    className="shrink-0 text-amber-500"
                                                    aria-label={GOAL_GAP_LABEL[gap]}
                                                />
                                            </Tooltip>
                                        )}
                                    </span>
                                </td>
                                {weeks.map((w, i) => {
                                    const key = weekKey(w);
                                    const editable = canEditWeek ? canEditWeek(key) : true;
                                    return (
                                        <td key={i} className="px-2 py-2 text-center">
                                            <ScorecardCell
                                                metric={m}
                                                rawValue={valueAt(m.id, key)}
                                                note={noteAt ? noteAt(m.id, key) : null}
                                                editable={editable}
                                                onSave={v => onSaveCell(m.id, w, v)}
                                                onSaveNote={
                                                    editable && onSaveNote
                                                        ? n => onSaveNote(m.id, w, n)
                                                        : null
                                                }
                                            />
                                        </td>
                                    );
                                })}
                                <td
                                    className={`${FROZEN} ${RIGHT_EDGE} px-3 py-2 text-center bg-gray-50 group-hover:bg-gray-100 border-l border-gray-200 font-mono text-sm ${avgColour(avg4Met)}`}
                                    style={{ right: `${RIGHT.avg4}rem` }}
                                >
                                    {avg4 !== null ? (
                                        formatScorecardValue(avg4, m.value_type)
                                    ) : (
                                        <span className="text-gray-300">—</span>
                                    )}
                                </td>
                                <td
                                    className={`${FROZEN} px-3 py-2 text-center bg-gray-50 group-hover:bg-gray-100 font-mono text-sm ${avgColour(avg13Met)}`}
                                    style={{ right: `${RIGHT.avg13}rem` }}
                                >
                                    {avg13 !== null ? (
                                        formatScorecardValue(avg13, m.value_type)
                                    ) : (
                                        <span className="text-gray-300">—</span>
                                    )}
                                </td>
                                {rowActions && (
                                    <td
                                        className={`${FROZEN} px-2 py-2 text-right bg-gray-50 group-hover:bg-gray-100`}
                                        style={{ right: `${RIGHT.actions}rem` }}
                                    >
                                        {rowActions(m)}
                                    </td>
                                )}
                            </tr>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
};
