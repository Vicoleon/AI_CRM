// The three grids: your own shape, a pair's overlap, and a team's coverage.
//
// All three obey the same two rules, which live in `heatmapCells.ts` and in
// `rubik_core/smart_teams/heatmap.py`:
//
//   * a number appears only where the server sent one, and it sends one only to
//     a viewer holding the raw-profile gate for that cell's subject;
//   * "not measured" is drawn as a hatch and never as a fill, because it is the
//     opposite finding from "you match" and a heatmap is exactly the format that
//     invites collapsing the two into one grey.
//
// COLOUR IS NEVER THE ONLY CHANNEL
// --------------------------------
// Every cell carries its own text — a band word, a state word, a tick — and an
// `aria-label` that states the fact in full. That is not decoration: a grid
// whose meaning is carried by hue alone is unreadable to a colour-blind reader
// and invisible to a screen reader, and this one is describing people.
//
// NO TOTALS, NO SORTING, NO ORDERING OF PEOPLE
// --------------------------------------------
// The coverage grid deliberately has no per-person column total and no sortable
// header. Either would turn a factual "who confirmed what" matrix into a ranking
// of colleagues by count, which is what `team_contracts._forbid_person_metrics`
// exists to prevent server-side. Column order is roster order and the caption
// says so.
import React from 'react';
import { Check, Info, Minus } from 'lucide-react';

import type {
    HeatmapCell,
    OwnHeatmapResponse,
    PairHeatmapResponse,
    TeamAnswerResponse,
} from '../../../api/smartTeams';
import { TeamMap } from './TeamMap';
import {
    OVERLAP_LEGEND,
    bandClass,
    bandLabel,
    groupByFramework,
    hasScore,
    overlapStyle,
    scoreLabel,
    summarise,
} from './heatmapCells';

// ── Shared chrome ────────────────────────────────────────────────────────────

const Section: React.FC<{
    title: string;
    hint?: React.ReactNode;
    children: React.ReactNode;
}> = ({ title, hint, children }) => (
    <section className="rounded-lg border border-gray-200 bg-white p-5">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        {hint && <p className="mt-1 text-xs leading-relaxed text-gray-500">{hint}</p>}
        <div className="mt-3">{children}</div>
    </section>
);

/** The overlap key. Rendered on every pair grid — the hatch needs explaining
 *  once, where the reader is looking, not in a tooltip they may never open. */
export const OverlapLegend: React.FC = () => (
    <ul className="flex flex-wrap gap-x-4 gap-y-2">
        {OVERLAP_LEGEND.map(state => {
            const style = overlapStyle(state);
            return (
                <li key={state} className="flex items-center gap-1.5">
                    <span
                        aria-hidden="true"
                        className={`inline-block h-3.5 w-3.5 flex-shrink-0 rounded border ${style.box}`}
                    />
                    <span className="text-xs text-gray-600">{style.label}</span>
                </li>
            );
        })}
    </ul>
);

/** Wraps a wide grid so the PAGE never scrolls sideways — the table does. */
const Scroller: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <div className="-mx-1 overflow-x-auto px-1">{children}</div>
);

// ── 1. Your own shape ────────────────────────────────────────────────────────

const OwnRow: React.FC<{ cell: HeatmapCell; showNumbers: boolean }> = ({
    cell,
    showNumbers,
}) => {
    const label = bandLabel(cell.band);
    const number = scoreLabel(cell);
    return (
        <tr className="border-t border-gray-100">
            <th
                scope="row"
                className="py-2 pr-3 text-left text-sm font-normal text-gray-700"
            >
                {cell.name}
            </th>
            <td className="py-2">
                <div
                    className={`flex items-center justify-between gap-3 rounded-md px-3 py-1.5 ${bandClass(cell)}`}
                    aria-label={`${cell.name}: ${label}${
                        showNumbers && number ? `, ${number} out of 100` : ''
                    }`}
                >
                    <span className="text-xs font-medium">{label}</span>
                    {showNumbers && number && (
                        <span className="font-mono text-xs tabular-nums opacity-80">
                            {number}
                        </span>
                    )}
                </div>
            </td>
        </tr>
    );
};

export const OwnHeatmap: React.FC<{ data: OwnHeatmapResponse }> = ({ data }) => {
    if (data.state === 'not_scored') {
        return (
            <Section title="Your shape at a glance">
                <p className="text-sm text-gray-500">
                    Nothing to draw yet — this fills in once your interview has been
                    scored.
                </p>
            </Section>
        );
    }
    if (!data.cells.length) {
        return (
            <Section title="Your shape at a glance">
                <p className="text-sm text-gray-500">
                    Your answers did not support reporting any of the scales. That is a
                    real answer rather than a gap: the interview would rather say nothing
                    than describe you from too little.
                </p>
            </Section>
        );
    }

    const groups = groupByFramework(data.cells);
    return (
        <>
        {data.style_marker && (
            <Section
                title="Where you sit"
                hint="Your working style on the two axes the interview measures — how much you push for progress, and how much you work through people. There is no better or worse place to be on this."
            >
                <TeamMap
                    dots={[{ id: 'me', label: 'You', marker: data.style_marker, emphasis: true }]}
                />
            </Section>
        )}
        <Section
            title="Your shape at a glance"
            hint={
                data.shows_numbers
                    ? 'Your own numbers, which only you and the admins who support Smart Teams can see. Colleagues see the words, never the figures.'
                    : 'Where you sit on each scale. There is no good or bad end of any of these.'
            }
        >
            <div className="space-y-5">
                {groups.map(group => (
                    <div key={group.framework}>
                        <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-400">
                            {group.label}
                        </h4>
                        {group.framework === 'motivators' && (
                            <p className="mb-2 text-xs leading-relaxed text-gray-500">
                                This one is an order, not a level. It says which of these
                                wins when two of them pull against each other — so it is
                                only ever about you, and it is never compared with a
                                colleague&apos;s.
                            </p>
                        )}
                        <table className="w-full table-fixed">
                            <caption className="sr-only">
                                {group.label}: your band on each dimension
                            </caption>
                            <colgroup>
                                <col className="w-2/5" />
                                <col />
                            </colgroup>
                            <tbody>
                                {group.rows.map(cell => (
                                    <OwnRow
                                        key={`${cell.framework}-${cell.key}`}
                                        cell={cell}
                                        showNumbers={data.shows_numbers && hasScore(cell)}
                                    />
                                ))}
                            </tbody>
                        </table>
                    </div>
                ))}
            </div>
        </Section>
        </>
    );
};

// ── 2. A pair's overlap ──────────────────────────────────────────────────────

const PairCellBox: React.FC<{ cell: HeatmapCell | null }> = ({ cell }) => {
    if (!cell) {
        return (
            <span className="block rounded-md border border-dashed border-gray-300 px-2 py-1.5 text-center text-xs text-gray-400">
                —
            </span>
        );
    }
    const number = scoreLabel(cell);
    return (
        <span
            className={`flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-center text-xs font-medium ${bandClass(cell)}`}
        >
            {bandLabel(cell.band)}
            {number && (
                <span className="font-mono tabular-nums opacity-75">{number}</span>
            )}
        </span>
    );
};

export const PairHeatmap: React.FC<{ data: PairHeatmapResponse }> = ({ data }) => {
    const you = 'You';
    const them =
        data.display_names[data.about_email] || data.about_email.split('@')[0];

    if (!data.both_scored) {
        return (
            <Section title="Where you overlap">
                <p className="text-sm text-gray-500">
                    One of you has not been scored yet, so there is nothing to line up.
                    This fills in once both interviews have been scored.
                </p>
            </Section>
        );
    }

    const groups = groupByFramework(data.rows);
    const readerMarker = data.markers?.[data.reader_email] ?? null;
    const aboutMarker = data.markers?.[data.about_email] ?? null;
    return (
        <>
        {(readerMarker || aboutMarker) && (
            <Section
                title="The two of you on the map"
                hint="Close together means you push and connect in similar ways; far apart is where you will need to meet in the middle. Neither position is the better one."
            >
                <TeamMap
                    dots={[
                        { id: 'reader', label: you, marker: readerMarker, emphasis: true },
                        { id: 'about', label: them, marker: aboutMarker },
                    ]}
                />
            </Section>
        )}
        <Section
            title="Where you overlap, and where you pull apart"
            hint="Bands, not numbers — a colleague never sees your figures, and you never see theirs. Nothing here is a score of how well you work together."
        >
            <p className="mb-3 text-sm leading-relaxed text-gray-700">
                {summarise(data.rows)}
            </p>

            <Scroller>
                <table className="w-full min-w-[30rem] border-separate border-spacing-y-1">
                    <caption className="sr-only">
                        Your band and {them}&apos;s band on each dimension, with whether
                        they line up
                    </caption>
                    <thead>
                        <tr className="text-left text-xs font-medium uppercase tracking-wide text-gray-400">
                            <th scope="col" className="w-1/3 pb-1">
                                Dimension
                            </th>
                            <th scope="col" className="w-1/4 pb-1">
                                {you}
                            </th>
                            <th scope="col" className="w-1/4 pb-1">
                                {them}
                            </th>
                            <th scope="col" className="pb-1">
                                Together
                            </th>
                        </tr>
                    </thead>
                    {groups.map(group => (
                        <tbody key={group.framework}>
                            <tr>
                                <th
                                    scope="colgroup"
                                    colSpan={4}
                                    className="pt-2 text-left text-xs font-medium uppercase tracking-wide text-gray-400"
                                >
                                    {group.label}
                                </th>
                            </tr>
                            {group.rows.map(row => {
                                const style = overlapStyle(row.overlap);
                                return (
                                    <tr key={`${row.framework}-${row.key}`}>
                                        <th
                                            scope="row"
                                            className="pr-3 text-left text-sm font-normal text-gray-700"
                                        >
                                            {row.name}
                                        </th>
                                        <td className="pr-1.5">
                                            <PairCellBox cell={row.reader} />
                                        </td>
                                        <td className="pr-1.5">
                                            <PairCellBox cell={row.about} />
                                        </td>
                                        <td>
                                            <span
                                                title={style.meaning}
                                                aria-label={`${row.name}: ${style.label}. ${style.meaning}`}
                                                className={`block rounded-md border px-2 py-1.5 text-center text-xs font-medium ${style.box}`}
                                            >
                                                {style.label}
                                            </span>
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    ))}
                </table>
            </Scroller>

            <div className="mt-3 border-t border-gray-100 pt-3">
                <OverlapLegend />
                <p className="mt-2 flex items-start gap-1.5 text-xs leading-relaxed text-gray-500">
                    <Info size={13} className="mt-0.5 flex-shrink-0" />
                    <span>
                        <span className="font-medium">Not measured</span> is not
                        agreement — it means one of you has nothing reportable on that
                        dimension, so there was nothing to compare.
                    </span>
                </p>
            </div>
        </Section>
        </>
    );
};

// ── 3. A team's coverage ─────────────────────────────────────────────────────

export const CoverageHeatmap: React.FC<{ answer: TeamAnswerResponse }> = ({
    answer,
}) => {
    const people = answer.group;
    const skills = answer.coverage;
    if (!people.length || !skills.length) return null;

    const covers = (skillKey: string, email: string) =>
        skills
            .find(item => item.skill === skillKey)
            ?.covered_by.some(who => who.toLowerCase() === email.toLowerCase()) ?? false;

    return (
        <Section
            title="Who covers what"
            hint="Everyone who confirmed each skill. Columns are in roster order — it is not a ranking, and there is no primary holder of a skill."
        >
            <Scroller>
                <table className="w-full min-w-[30rem] text-sm">
                    <caption className="sr-only">
                        Skills asked for, against who on this team confirmed each one
                    </caption>
                    <thead>
                        <tr>
                            <th
                                scope="col"
                                className="pb-2 pr-3 text-left text-xs font-medium uppercase tracking-wide text-gray-400"
                            >
                                Skill
                            </th>
                            {people.map(person => (
                                <th
                                    key={person.email}
                                    scope="col"
                                    className="px-1 pb-2 text-center text-xs font-medium text-gray-500"
                                >
                                    <span className="block truncate" title={person.name}>
                                        {person.name.split(' ')[0]}
                                    </span>
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {skills.map(skill => (
                            <tr key={skill.skill} className="border-t border-gray-100">
                                <th
                                    scope="row"
                                    className="py-1.5 pr-3 text-left text-sm font-normal text-gray-700"
                                >
                                    {skill.name}
                                    {skill.required && (
                                        <span className="ml-1.5 text-[11px] text-gray-400">
                                            must-have
                                        </span>
                                    )}
                                </th>
                                {people.map(person => {
                                    const yes = covers(skill.skill, person.email);
                                    return (
                                        <td key={person.email} className="px-1 py-1.5">
                                            <span
                                                aria-label={`${person.name} ${
                                                    yes ? 'confirmed' : 'has not confirmed'
                                                } ${skill.name}`}
                                                className={`mx-auto flex h-6 w-full max-w-[3rem] items-center justify-center rounded ${
                                                    yes
                                                        ? 'bg-brand/80 text-white'
                                                        : 'bg-gray-100 text-gray-300'
                                                }`}
                                            >
                                                {yes ? (
                                                    <Check size={13} aria-hidden="true" />
                                                ) : (
                                                    <Minus size={13} aria-hidden="true" />
                                                )}
                                            </span>
                                        </td>
                                    );
                                })}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </Scroller>
            {/* No column totals, deliberately — see the file header. */}
            <p className="mt-3 text-xs leading-relaxed text-gray-500">
                A blank cell means that person has not confirmed that skill in Smart
                Teams. It does not mean they cannot do it.
            </p>
        </Section>
    );
};
