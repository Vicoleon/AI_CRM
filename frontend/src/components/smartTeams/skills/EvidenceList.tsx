// What Cubie read, and why it concluded a skill from it.
//
// PRIVATE TO THE SUBJECT, BY THE TYPE THIS COMPONENT TAKES
// -------------------------------------------------------
// The props are `SkillEvidenceItem[]` — meeting titles, document names and
// transcript excerpts gathered AS the subject, so none of it has passed
// `meeting_participants` or a document ACL on the way to a third party's
// screen. `GET /skills/people/{email}` has no evidence field on its rows at all
// (`skills.ConfirmedSkill` is a DTO with no such attribute), so a colleague-
// facing caller has nothing it could hand this component. That is the guarantee:
// not a flag this file checks, but an argument the colleague read cannot supply.
//
// Collapsed by default and opened per skill. A person answering six suggestions
// wants to see one at a time, and every citation carries a `basis` caveat about
// what the source actually proves — text that is only readable one card at a time.
import React, { useId, useState } from 'react';
import { ChevronDown, ChevronRight, Quote } from 'lucide-react';

import type { SkillEvidenceItem } from '../../../api/smartTeams';
import { EvidenceKindIcon } from './skillsShared';
import { evidenceKindLabel, shortDate } from './skillsFormat';

interface EvidenceListProps {
    /** `private_dict.evidence`. */
    items: SkillEvidenceItem[];
    /** `private_dict.rationale` — what the model said it concluded, screened for
     *  advisory language before it was ever stored. `''` for a self-declared
     *  skill, which is why this component is not rendered on one. */
    rationale: string;
    /** Label for the toggle, so a screen reader hears which skill it opens. */
    skillName: string;
}

export const EvidenceList: React.FC<EvidenceListProps> = ({ items, rationale, skillName }) => {
    const [open, setOpen] = useState(false);
    const panelId = useId();

    // Both empty is a real state: a row can exist with a rationale and no
    // citations, or neither, and pretending otherwise would put an empty
    // disclosure on the card. Saying it plainly matters more here than anywhere
    // else on the page — "Cubie suggested this and cannot show you why" is
    // exactly the thing a person is entitled to know before answering.
    if (items.length === 0 && !rationale.trim()) {
        return (
            <p className="mt-2 text-xs leading-relaxed text-gray-500">
                Cubie has no record of what this suggestion was based on. There is nothing to
                show you here, so treat it as a guess with no working attached — and say no if
                that is the right answer.
            </p>
        );
    }

    return (
        <div className="mt-2">
            <button
                type="button"
                onClick={() => setOpen(value => !value)}
                aria-expanded={open}
                aria-controls={panelId}
                className="inline-flex items-center gap-1.5 rounded text-xs font-medium text-brand hover:underline focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-1"
            >
                {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                {open ? 'Hide' : 'Show'} what this was based on
                <span className="sr-only"> for {skillName}</span>
                {items.length > 0 && (
                    <span className="font-normal text-gray-500">
                        ({items.length} {items.length === 1 ? 'source' : 'sources'})
                    </span>
                )}
            </button>

            {open && (
                <div id={panelId} className="mt-2 space-y-3 border-l-2 border-indigo-200 pl-3">
                    {rationale.trim() && (
                        <p className="flex items-start gap-1.5 text-xs leading-relaxed text-gray-700">
                            <Quote className="mt-0.5 flex-shrink-0 text-gray-400" size={12} />
                            <span>{rationale}</span>
                        </p>
                    )}

                    {items.length === 0 ? (
                        <p className="text-xs leading-relaxed text-gray-500">
                            No individual sources are recorded against this one.
                        </p>
                    ) : (
                        <ul className="space-y-2.5">
                            {items.map((item, index) => {
                                const when = shortDate(item.when);
                                return (
                                    <li
                                        key={`${item.kind}:${item.ref}:${index}`}
                                        className="rounded-md border border-gray-200 bg-white p-2.5"
                                    >
                                        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500">
                                            <span className="inline-flex items-center gap-1 font-medium text-gray-700">
                                                <EvidenceKindIcon kind={item.kind} />
                                                {evidenceKindLabel(item.kind)}
                                            </span>
                                            {when && <span>· {when}</span>}
                                        </p>
                                        <p className="mt-1 text-xs font-medium text-gray-900">
                                            {item.title}
                                        </p>
                                        {item.phrase && (
                                            <p className="mt-1 text-xs text-gray-600">
                                                Matched on{' '}
                                                <span className="font-medium text-gray-800">
                                                    “{item.phrase}”
                                                </span>
                                            </p>
                                        )}
                                        {item.excerpt && (
                                            <blockquote className="mt-1.5 border-l-2 border-gray-200 pl-2 text-xs italic leading-relaxed text-gray-600">
                                                {item.excerpt}
                                            </blockquote>
                                        )}
                                        {/* The caveat travels with the citation
                                            because it bounds what the source
                                            proves — a meeting says you were
                                            listed on it, not that you spoke.
                                            Server copy, verbatim. */}
                                        {item.basis && (
                                            <p className="mt-1.5 text-[11px] leading-relaxed text-gray-500">
                                                {item.basis}
                                            </p>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </div>
            )}
        </div>
    );
};
