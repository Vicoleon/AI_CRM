// The Enneagram confirmation step — the one place in the interview where what
// the person says overrides what Cubie worked out.
//
// Four things this screen has to get right, all of them from the instrument's
// own design notes:
//
//   * NOTHING labels either description as Cubie's pick. The person chooses
//     between plain-language descriptions, not between agreeing and disagreeing
//     with us. The option strings come from the server verbatim
//     (`enneagram.FIRST_OPTION` and friends) for exactly that reason, and the
//     question is the instrument's sentence — including the "not enough answers
//     to suggest a type" one, which arrives with an EMPTY candidate list and
//     must never be dressed up as a finding.
//   * OPTIONS RENDER IN THE ORDER RECEIVED, and the chosen option is sent back
//     as the exact string it arrived as. `conversation.enneagram_answer` maps an
//     option to a type BY POSITION, so re-sorting this list would write down a
//     type the person did not choose. The role of each option is derived from
//     its position the same way the server derives it — candidates first, then
//     "let me pick from the nine", then "skip" — rather than from matching
//     instrument copy in the browser.
//   * "Neither" is a real answer, and it opens the whole list of nine rather
//     than pushing the person back to what we offered.
//   * Declining is a real answer too, and it is stored as one (`{declined:
//     true}`), which is what lets the profile say "asked, declined" instead of
//     going quiet.
//
// A WING IS NOT OFFERED HERE, and that is a reported gap rather than a decision:
// the server validates a wing against `enneagram.adjacent(type)` and does not
// publish the adjacency set, so the only way to offer a picker would be to
// re-implement instrument arithmetic in the browser and hope. Picking one of the
// suggested descriptions carries the questionnaire's own inferred wing across
// server-side; picking from the nine stores no wing.
import React, { useMemo, useState } from 'react';
import { Check, Info } from 'lucide-react';

import type {
    ConfirmationState,
    EnneagramConfirmationAnswer,
    StructuredPayload,
} from '../../api/smartTeams';
import { PrimaryButton } from './shared';

/** Where an option sits in the list decides what it means — the same positional
 *  rule `enneagram_answer` applies: index < candidates is a candidate, the next
 *  one opens the nine, the last one declines. */
type OptionRole = 'candidate' | 'other' | 'decline';

function roleFor(index: number, candidateCount: number): OptionRole {
    if (index < candidateCount) return 'candidate';
    return index === candidateCount ? 'other' : 'decline';
}

const ORDINALS = ['The first one', 'The second one', 'The third one', 'The fourth one'];

interface ConfirmationStepProps {
    confirmation: ConfirmationState;
    /** All nine patterns, for the "let me pick from the nine" branch. */
    types: StructuredPayload['types'];
    /** The stored answer, when they have been here before. */
    value: EnneagramConfirmationAnswer | null;
    /** Sends the option string exactly as received, plus the type when the
     *  option is the pick-from-the-nine one. */
    onSubmit: (option: string, typeNumber: number | null) => void;
    submitting: boolean;
}

export const ConfirmationStep: React.FC<ConfirmationStepProps> = ({
    confirmation,
    types,
    value,
    onSubmit,
    submitting,
}) => {
    const candidateCount = confirmation.candidates.length;

    const [optionIndex, setOptionIndex] = useState<number | null>(null);
    const [otherType, setOtherType] = useState<number | null>(
        value && !value.declined ? value.type_number : null,
    );

    const role = optionIndex === null ? null : roleFor(optionIndex, candidateCount);
    const ready =
        optionIndex !== null && (role !== 'other' || otherType !== null);

    const answeredLabel = useMemo(() => {
        if (!value) return null;
        if (value.declined) return 'You chose to leave this layer off your profile.';
        const match = types.find(t => t.number === value.type_number);
        return match
            ? `Your answer on record: ${match.label}.`
            : 'You have already answered this section.';
    }, [types, value]);

    const submit = () => {
        if (optionIndex === null || !ready) return;
        onSubmit(
            confirmation.options[optionIndex],
            role === 'other' ? otherType : null,
        );
    };

    return (
        <div className="space-y-4">
            <div className="rounded-lg border border-gray-200 bg-white p-4">
                <p className="text-sm font-medium leading-relaxed text-gray-900">
                    {confirmation.question}
                </p>
                <p className="mt-2 text-xs leading-relaxed text-gray-500">
                    {confirmation.footnote}
                </p>
            </div>

            {answeredLabel && (
                <p className="flex items-start gap-2 rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600">
                    <Info className="mt-0.5 flex-shrink-0 text-gray-400" size={14} />
                    {answeredLabel} Answering again replaces it.
                </p>
            )}

            {/* The descriptions, in plain language, in the order the server sent
                them. No badge, no "our guess" marker, and nothing at all when
                there were too few answers to suggest any. */}
            {candidateCount > 0 && (
                <ol className="space-y-3">
                    {confirmation.candidates.map((candidate, index) => (
                        <li
                            key={candidate.type_number}
                            className="rounded-lg border border-gray-200 bg-white p-4"
                        >
                            <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-brand">
                                {ORDINALS[index] ?? `Description ${index + 1}`}
                            </p>
                            <p className="text-sm leading-relaxed text-gray-800">
                                {candidate.plain_language}
                            </p>
                        </li>
                    ))}
                </ol>
            )}

            <fieldset className="rounded-lg border border-gray-200 bg-white p-4">
                <legend className="px-1 text-sm font-medium text-gray-900">Your answer</legend>
                <div className="mt-2 space-y-2">
                    {confirmation.options.map((option, index) => (
                        <label
                            key={option}
                            htmlFor={`st-conf-${index}`}
                            className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                optionIndex === index
                                    ? 'border-brand bg-brand-tint text-brand-hover'
                                    : 'border-gray-200 bg-white text-gray-700 hover:border-brand'
                            }`}
                        >
                            <input
                                id={`st-conf-${index}`}
                                type="radio"
                                name="st-confirmation"
                                checked={optionIndex === index}
                                onChange={() => setOptionIndex(index)}
                                className="mt-0.5 h-4 w-4 accent-brand"
                            />
                            <span className="leading-relaxed">{option}</span>
                        </label>
                    ))}
                </div>
            </fieldset>

            {role === 'other' && (
                <fieldset className="rounded-lg border border-gray-200 bg-white p-4">
                    <legend className="px-1 text-sm font-medium text-gray-900">
                        Which one, then?
                    </legend>
                    <p className="mb-3 mt-1 text-xs text-gray-500">
                        Whatever you pick here is what your profile says. Cubie does not argue
                        with it.
                    </p>
                    <div className="space-y-2">
                        {types.map(type => (
                            <label
                                key={type.number}
                                htmlFor={`st-type-${type.number}`}
                                className={`flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm transition-colors focus-within:ring-2 focus-within:ring-brand focus-within:ring-offset-1 ${
                                    otherType === type.number
                                        ? 'border-brand bg-brand-tint text-brand-hover'
                                        : 'border-gray-200 bg-white text-gray-700 hover:border-brand'
                                }`}
                            >
                                <input
                                    id={`st-type-${type.number}`}
                                    type="radio"
                                    name="st-type"
                                    checked={otherType === type.number}
                                    onChange={() => setOtherType(type.number)}
                                    className="mt-0.5 h-4 w-4 accent-brand"
                                />
                                <span className="leading-relaxed">{type.label}</span>
                            </label>
                        ))}
                    </div>
                </fieldset>
            )}

            <div className="flex justify-end">
                <PrimaryButton onClick={submit} busy={submitting} disabled={!ready}>
                    <Check size={15} />
                    Save and finish this section
                </PrimaryButton>
            </div>
        </div>
    );
};
