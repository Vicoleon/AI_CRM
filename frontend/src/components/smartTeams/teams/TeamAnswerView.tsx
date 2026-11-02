// One Team Builder answer, rendered.
//
// THE THREE THINGS THIS SCREEN SHOWS ARE ALL PROPERTIES OF THE GROUP
// -----------------------------------------------------------------
// Coverage (which of the skills asked for are covered, and by whom), balance
// (which kinds of contribution and which working styles the group has between
// them), and availability (whose booked time off overlaps the window). None of
// them is a judgement of a person, and there is no per-person number in
// `TeamAnswerResponse` to render one from.
//
// So this component:
//   * orders the group alphabetically and offers no other order (`grouping.ts`
//     has the argument);
//   * lists what each person has confirmed WITHOUT a count — "covers 3 of 4" is
//     a per-person score with a different name, and `_forbid_person_metrics`
//     exists to keep one out of the payload;
//   * renders no badge, medal, bar, percentage, star or "top match";
//   * does not render `TeamMember.work_style`. The router sends the raw DISC
//     style KEY (`"D"`, `"D+I"`, `"BALANCED"`) with no human label beside it, and
//     printing a bare code at a person is both unreadable and the one thing the
//     type rules forbid. Working style is read at the group level, from
//     `style_balance` and the server's own write-up.
//
// AN UNSATISFIABLE ASK IS AN ANSWER
// ---------------------------------
// `POST /teams` is 200 whenever the ask parses. `state: 'empty'` with
// `shortfalls` is the truthful answer to "nobody has confirmed that skill", and
// `state: 'partial'` means the group does not cover everything asked for. Both
// get their own banner, and a partial group is never rendered as though it were
// complete — the uncovered skills are named next to it, above the group, not in
// a footnote.
import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
    AlertTriangle,
    CalendarOff,
    CircleHelp,
    EyeOff,
    Info,
    Layers,
    Users,
    Wrench,
} from 'lucide-react';

import type { TeamAnswerResponse, TeamMember } from '../../../api/smartTeams';
import { AdvisoryNotice } from '../shared';
import { CoverageHeatmap } from '../heatmap/Heatmap';
import { TeamMap } from '../heatmap/TeamMap';
import {
    availabilityTally,
    AVAILABILITY_LABELS,
    coverageTally,
    orderedGroup,
} from './grouping';

const formatDate = (iso: string): string => {
    const parsed = new Date(`${iso}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) return iso;
    return parsed.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
    });
};

const Card: React.FC<{
    icon: React.ReactNode;
    title: string;
    children: React.ReactNode;
    tone?: 'plain' | 'muted' | 'unassessed';
}> = ({ icon, title, children, tone = 'plain' }) => (
    <section
        className={`rounded-xl border p-5 ${
            tone === 'unassessed'
                ? 'border-dashed border-slate-300 bg-slate-50'
                : tone === 'muted'
                  ? 'border-gray-200 bg-gray-50'
                  : 'border-gray-200 bg-white'
        }`}
    >
        <h3 className="mb-3 flex items-center gap-2 text-base font-semibold text-gray-900">
            <span
                aria-hidden="true"
                className={tone === 'unassessed' ? 'text-slate-500' : 'text-brand'}
            >
                {icon}
            </span>
            {title}
        </h3>
        {children}
    </section>
);

// ── The one person row ───────────────────────────────────────────────────────

const MemberRow: React.FC<{ member: TeamMember }> = ({ member }) => (
    <li className="px-4 py-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-sm font-medium text-gray-900">
                {member.name || member.email}
            </span>
            <span className="inline-flex items-center gap-1.5 text-xs text-gray-600">
                {member.availability === 'unknown' ? (
                    <CircleHelp size={13} aria-hidden="true" className="text-gray-400" />
                ) : member.availability === 'available' ? null : (
                    <CalendarOff size={13} aria-hidden="true" className="text-amber-600" />
                )}
                {AVAILABILITY_LABELS[member.availability] ?? member.availability}
                {member.back_on && <> · back {formatDate(member.back_on)}</>}
            </span>
        </div>
        {member.confirmed.length > 0 && (
            <p className="mt-1.5 text-xs leading-relaxed text-gray-600">
                <span className="text-gray-500">Confirmed, from this ask: </span>
                {/* Names only. No count, and no ordering by how many. */}
                {member.confirmed.map(item => item.name).join(' · ')}
            </p>
        )}
    </li>
);

// ── The answer ───────────────────────────────────────────────────────────────

export interface TeamAnswerViewProps {
    answer: TeamAnswerResponse;
}

export const TeamAnswerView: React.FC<TeamAnswerViewProps> = ({ answer }) => {
    const group = orderedGroup(answer.group);
    const tally = coverageTally(answer);
    const availability = availabilityTally(group);
    const names = new Map(group.map(member => [member.email, member.name || member.email]));
    const nameFor = (email: string) => names.get(email) ?? email;

    return (
        <div className="space-y-4">
            {/* ── State: never a partial group dressed as a complete one ──── */}
            {answer.state === 'empty' && (
                <div
                    role="status"
                    className="rounded-xl border border-amber-200 bg-amber-50 p-5"
                >
                    <p className="flex items-center gap-2 text-sm font-semibold text-amber-900">
                        <AlertTriangle size={16} aria-hidden="true" />
                        Cubie cannot put a group together for this ask
                    </p>
                    <p className="mt-1.5 text-sm leading-relaxed text-amber-800">
                        This is an answer, not a failure. What is missing, and what would
                        change it, is below.
                    </p>
                </div>
            )}

            {answer.state === 'partial' && (
                <div
                    role="status"
                    className="rounded-xl border border-amber-200 bg-amber-50 p-5"
                >
                    <p className="flex items-center gap-2 text-sm font-semibold text-amber-900">
                        <AlertTriangle size={16} aria-hidden="true" />
                        This group does not cover everything you asked for
                    </p>
                    {tally.uncoveredRequired.length > 0 && (
                        <p className="mt-1.5 text-sm leading-relaxed text-amber-800">
                            Not covered:{' '}
                            <span className="font-medium">
                                {tally.uncoveredRequired.map(item => item.name).join(', ')}
                            </span>
                            .
                        </p>
                    )}
                    <p className="mt-1.5 text-sm leading-relaxed text-amber-800">
                        Cubie does not pad a group with people it has no reason to include,
                        so read this as a group that covers part of the ask.
                    </p>
                </div>
            )}

            {/* ── Phrases Cubie does not track. Reported, never coerced. ──── */}
            {answer.unrecognised.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-5">
                    <p className="text-sm font-semibold text-amber-900">
                        Cubie does not track{' '}
                        {answer.unrecognised.map(phrase => `“${phrase}”`).join(', ')}
                    </p>
                    <p className="mt-1.5 text-sm leading-relaxed text-amber-800">
                        Nothing was filtered on those. They were not quietly matched onto a
                        similar skill — that is how somebody ends up staffed onto work they
                        have never touched.
                    </p>
                </div>
            )}

            {/* ── What is missing, and what would fix it ──────────────────── */}
            {answer.shortfalls.length > 0 && (
                <Card icon={<Wrench size={16} />} title="What is missing, and what would fix it">
                    <ul className="space-y-3">
                        {answer.shortfalls.map((shortfall, index) => (
                            <li key={`${shortfall.code}-${index}`}>
                                <p className="text-sm leading-relaxed text-gray-700">
                                    {shortfall.detail}
                                </p>
                            </li>
                        ))}
                    </ul>
                    {answer.horizon_note && (
                        <p className="mt-3 border-t border-gray-100 pt-3 text-sm leading-relaxed text-gray-600">
                            {answer.horizon_note}
                        </p>
                    )}
                </Card>
            )}

            {/* ── The ask, echoed ─────────────────────────────────────────── */}
            {answer.asked_for.length > 0 && (
                <Card icon={<Layers size={16} />} title="Skill coverage" tone="plain">
                    <p className="mb-3 text-xs text-gray-500">
                        {tally.requiredCovered} of {tally.required} needed
                        {tally.optional > 0 && (
                            <>
                                {' '}
                                · {tally.optionalCovered} of {tally.optional} nice-to-have
                            </>
                        )}{' '}
                        covered. Counts are about the ask, not about anybody in the group.
                    </p>
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <caption className="sr-only">
                                Each skill asked for, whether the group covers it, and who
                                has confirmed it
                            </caption>
                            <thead>
                                <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                                    <th scope="col" className="py-2 pr-3 font-semibold">
                                        Skill
                                    </th>
                                    <th scope="col" className="py-2 pr-3 font-semibold">
                                        Asked for
                                    </th>
                                    <th scope="col" className="py-2 font-semibold">
                                        Confirmed by
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-gray-100">
                                {answer.coverage.map(item => (
                                    <tr key={item.skill}>
                                        <th
                                            scope="row"
                                            className="py-2.5 pr-3 align-top font-medium text-gray-900"
                                        >
                                            {item.name}
                                        </th>
                                        <td className="py-2.5 pr-3 align-top text-gray-600">
                                            {item.required ? 'Needed' : 'Nice to have'}
                                        </td>
                                        <td className="py-2.5 align-top text-gray-700">
                                            {item.covered ? (
                                                item.covered_by.map(nameFor).join(', ')
                                            ) : (
                                                <span className="text-amber-800">
                                                    Nobody in this group
                                                </span>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </Card>
            )}

            {/* The same coverage facts as the table above, as a grid — the list
                answers "is this skill covered", the grid answers "where does this
                team double up, and where does one person carry it alone".
                Deliberately no per-person totals and no sortable columns: either
                turns a factual matrix into a ranking of colleagues by count,
                which is what `team_contracts._forbid_person_metrics` refuses
                server-side. */}
            <CoverageHeatmap answer={answer} />

            {/* Named dots. `work_style` has always been per-person on the wire —
                it is what each member's OWN published guide says — and
                `_forbid_person_metrics` bans per-person JUDGEMENTS (score, rank,
                fit), not style identity. So this needed no guard loosened. The
                group-level `style_balance` below still answers "what is this team
                short of"; this answers "where does it bunch up". */}
            {group.length > 0 && (
                <Card icon={<Users size={16} />} title="The team on the map" tone="plain">
                    <TeamMap
                        caption="Where each person's own working style sits. Bunched together means the group will agree quickly and miss the same things; spread out means more friction and better coverage. There is no ideal shape, and this is not a ranking."
                        dots={group.map(member => ({
                            id: member.email,
                            label: member.name,
                            marker: member.style_marker,
                        }))}
                    />
                </Card>
            )}

            {/* ── The group ───────────────────────────────────────────────── */}
            {group.length > 0 && (
                <Card icon={<Users size={16} />} title="The group">
                    <p className="mb-1 text-xs leading-relaxed text-gray-500">
                        Listed alphabetically. This is one combination that covers the ask,
                        not a shortlist and not an order of preference.
                    </p>
                    {answer.window && (
                        <p className="mb-3 text-xs text-gray-500">
                            Time off checked for {formatDate(answer.window.start)} –{' '}
                            {formatDate(answer.window.end)}.
                        </p>
                    )}
                    <ul className="divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200">
                        {group.map(member => (
                            <MemberRow key={member.email} member={member} />
                        ))}
                    </ul>
                    {answer.window && (
                        <p className="mt-3 text-xs leading-relaxed text-gray-600">
                            {availability.available > 0 && (
                                <>
                                    {availability.available} with no booked time off in this
                                    window.{' '}
                                </>
                            )}
                            {availability.out_part_of_window > 0 && (
                                <>{availability.out_part_of_window} away for part of it. </>
                            )}
                            {availability.out_for_window > 0 && (
                                <>{availability.out_for_window} away for all of it. </>
                            )}
                            {availability.unknown > 0 && (
                                <>
                                    {availability.unknown} with no time-off record at all,
                                    which is not the same as being free.
                                </>
                            )}
                        </p>
                    )}
                </Card>
            )}

            {/* ── Balance: a group property, and the only place style is read ── */}
            <Card icon={<Layers size={16} />} title="How the group balances" tone="muted">
                {answer.style_balance.note && (
                    <p className="text-sm leading-relaxed text-gray-700">
                        {answer.style_balance.note}
                    </p>
                )}
                {answer.style_balance.clusters_covered.length > 0 && (
                    <div className="mt-3">
                        <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                            Kinds of contribution present between them
                        </p>
                        <ul className="mt-1.5 flex flex-wrap gap-2">
                            {answer.style_balance.clusters_covered.map(cluster => (
                                <li
                                    key={cluster}
                                    className="rounded-full bg-white px-2.5 py-1 text-xs text-gray-700 ring-1 ring-gray-200"
                                >
                                    {cluster}
                                </li>
                            ))}
                        </ul>
                    </div>
                )}
                {answer.style_balance.clusters_missing.length > 0 && (
                    <div className="mt-3">
                        <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                            Not present between them
                        </p>
                        <ul className="mt-1.5 flex flex-wrap gap-2">
                            {answer.style_balance.clusters_missing.map(cluster => (
                                <li
                                    key={cluster}
                                    className="rounded-full border border-dashed border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-600"
                                >
                                    {cluster}
                                </li>
                            ))}
                        </ul>
                    </div>
                )}
                {Object.entries(answer.style_balance.cluster_notes).length > 0 && (
                    <ul className="mt-3 space-y-1.5">
                        {Object.entries(answer.style_balance.cluster_notes).map(
                            ([cluster, note]) => (
                                <li
                                    key={cluster}
                                    className="text-sm leading-relaxed text-gray-700"
                                >
                                    {note}
                                </li>
                            ),
                        )}
                    </ul>
                )}
                {answer.style_balance.profiles_missing_for > 0 && (
                    <p className="mt-3 border-t border-gray-200 pt-3 text-sm leading-relaxed text-gray-600">
                        {answer.style_balance.profiles_missing_for} of the group have no
                        reportable working-style layer, so the mix above is read from the
                        rest. That is missing information rather than a finding about them.
                    </p>
                )}
            </Card>

            {/* ── What Cubie could NOT read about the group ───────────────── */}
            {(answer.unreadable_notes.length > 0 ||
                answer.dimension_readiness.length > 0) && (
                <Card
                    icon={<EyeOff size={16} />}
                    title="What Cubie could not read about this group"
                    tone="unassessed"
                >
                    <p className="mb-3 text-xs leading-relaxed text-slate-600">
                        These are counts of how much of the group each part of the profile
                        can be read on at all. A part nobody has is missing information, not
                        a finding that there is nothing to worry about — and there is no
                        per-person breakdown here on purpose, because that would publish who
                        has not finished their interview.
                    </p>
                    {answer.unreadable_notes.map((note, index) => (
                        <p
                            key={index}
                            className="mb-2 text-sm leading-relaxed text-slate-700"
                        >
                            {note}
                        </p>
                    ))}
                    {answer.dimension_readiness.length > 0 && (
                        <ul className="mt-3 grid gap-1.5 sm:grid-cols-2">
                            {answer.dimension_readiness.map(read => (
                                <li
                                    key={read.key}
                                    className="flex items-baseline justify-between gap-2 rounded-md bg-white px-2.5 py-1.5 text-xs ring-1 ring-slate-200"
                                >
                                    <span className="text-slate-700">{read.label}</span>
                                    <span
                                        className={
                                            read.open_for === 0
                                                ? 'flex-shrink-0 font-medium text-slate-500'
                                                : 'flex-shrink-0 text-slate-600'
                                        }
                                    >
                                        {read.open_for === 0
                                            ? 'nobody'
                                            : `${read.open_for} of ${read.of_people}`}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </Card>
            )}

            {/* ── The server's own write-up ───────────────────────────────── */}
            {answer.write_up.length > 0 && (
                <Card icon={<Info size={16} />} title="How Cubie read this ask">
                    <div className="space-y-2">
                        {answer.write_up.map((line, index) => (
                            <p
                                key={index}
                                className="text-sm leading-relaxed text-gray-700"
                            >
                                {line}
                            </p>
                        ))}
                    </div>
                </Card>
            )}

            {answer.notes.length > 0 && (
                <Card icon={<Info size={16} />} title="About this answer" tone="muted">
                    <ul className="space-y-2">
                        {answer.notes.map((note, index) => (
                            <li
                                key={index}
                                className="text-sm leading-relaxed text-gray-700"
                            >
                                {note}
                            </li>
                        ))}
                    </ul>
                </Card>
            )}

            {/* ── The notices that must not go missing ────────────────────── */}
            <div className="space-y-2">
                {[
                    answer.selection_notice,
                    answer.scope_notice,
                    answer.availability_notice,
                ]
                    .filter(Boolean)
                    .map((notice, index) => (
                        <p
                            key={index}
                            className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs leading-relaxed text-gray-600"
                        >
                            {notice}
                        </p>
                    ))}
            </div>

            <AdvisoryNotice text={answer.advisory_notice} />

            {/* `report` is the markdown the API also hands the Cubie Chat tool —
                its own docstring says "nothing here reaches a user directly: the
                model reads it and writes the answer", so it is written partly as
                instructions to that model. It is offered here, labelled for what
                it is, rather than presented as this page's prose. */}
            {answer.report && (
                <details className="rounded-xl border border-gray-200 bg-white p-4">
                    <summary className="cursor-pointer text-sm font-medium text-gray-700">
                        Show the raw text Cubie assembled for this answer
                    </summary>
                    <p className="mt-2 text-xs leading-relaxed text-gray-500">
                        This is the working text Cubie Chat reads to answer the same
                        question in conversation. It is written for that, so it reads as
                        notes and instructions rather than as finished copy.
                    </p>
                    <div className="prose prose-sm mt-3 max-w-none text-gray-700">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {answer.report}
                        </ReactMarkdown>
                    </div>
                </details>
            )}
        </div>
    );
};

export default TeamAnswerView;
