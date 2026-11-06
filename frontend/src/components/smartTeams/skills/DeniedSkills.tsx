// Skills the reader has said no to. A decision that stuck.
//
// WHY THIS SECTION EXISTS AT ALL
// ------------------------------
// A denial is a tombstone, not a delete: the row stays with `confirmed = false`
// and a marker in its evidence envelope, and both the candidate builder and the
// upsert read that marker — so the sweep genuinely cannot ask again. Two things
// follow for the UI, and they pull in opposite directions:
//
//   * the list must be visible, or a person who un-ticked something is looking at
//     data loss with no record of their own decision; and
//   * it must not read as a queue. Nothing here is pending, nothing is coming
//     back, and no button on this page will make Cubie re-suggest one.
//
// So it renders collapsed, in grey, with the server's own `denial_notice`, and the
// only action is an explicit "actually, add it" — which goes through
// `POST /skills/declare`, not through anything that looks like undoing a sweep.
// The server reports that lift in `undenied` and the page says so out loud;
// reversing a "no" silently would be worse than mentioning it.
import React, { useState } from 'react';
import { ChevronDown, ChevronRight, MinusCircle, Plus } from 'lucide-react';

import type { PrivateSkill } from '../../../api/smartTeams';
import { shortDate } from './skillsFormat';

interface DeniedSkillsProps {
    skills: PrivateSkill[];
    /** `_own_payload.denial_notice`, verbatim. */
    notice: string;
    /** Stage this skill in the declare panel. The only route back, and an
     *  explicit statement about oneself rather than an undo. */
    onReconsider: (skillKey: string) => void;
}

export const DeniedSkills: React.FC<DeniedSkillsProps> = ({ skills, notice, onReconsider }) => {
    const [open, setOpen] = useState(false);

    // Nothing denied is the common case and needs no section at all. An empty
    // grey box here would be a permanent reminder of a decision nobody made.
    if (skills.length === 0) return null;

    return (
        <section className="rounded-lg border border-gray-200 bg-gray-50 p-4">
            <button
                type="button"
                onClick={() => setOpen(value => !value)}
                aria-expanded={open}
                className="flex w-full items-center gap-2 rounded text-left focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-1"
            >
                {open ? (
                    <ChevronDown className="text-gray-500" size={15} />
                ) : (
                    <ChevronRight className="text-gray-500" size={15} />
                )}
                <MinusCircle className="text-gray-400" size={15} />
                <span className="text-sm font-medium text-gray-700">
                    You said no to {skills.length} {skills.length === 1 ? 'skill' : 'skills'}
                </span>
                <span className="text-xs text-gray-500">— Cubie will not ask again</span>
            </button>

            {open && (
                <div className="mt-3 space-y-3">
                    <p className="text-xs leading-relaxed text-gray-600">
                        {notice.trim() ||
                            'You said no to these, so Cubie will not suggest them again. They are listed only so you can change your mind — no colleague can see them.'}
                    </p>
                    <ul className="space-y-2">
                        {skills.map(skill => {
                            const when = shortDate(skill.denied_at);
                            return (
                                <li
                                    key={skill.skill_key}
                                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-gray-200 bg-white px-3 py-2"
                                >
                                    <span className="min-w-0">
                                        <span className="text-sm text-gray-700">{skill.name}</span>
                                        <span className="ml-2 text-xs text-gray-400">
                                            {skill.cluster ?? 'Other'}
                                            {when ? ` · ${when}` : ''}
                                        </span>
                                    </span>
                                    <button
                                        type="button"
                                        onClick={() => onReconsider(skill.skill_key)}
                                        className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-1"
                                    >
                                        <Plus size={12} />
                                        Add it after all
                                        <span className="sr-only"> — {skill.name}</span>
                                    </button>
                                </li>
                            );
                        })}
                    </ul>
                </div>
            )}
        </section>
    );
};
