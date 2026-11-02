// Ordering and counting rules for a Team Builder answer.
//
// THIS FILE EXISTS TO MAKE THE NO-RANKING RULE A SINGLE PLACE
// ----------------------------------------------------------
// `TeamAnswerResponse` has no per-person score, and that is enforced:
// `team_contracts._forbid_person_metrics` breaks the backend at import if a
// per-person number is ever added, and `_answer_dict` is built field by field
// rather than by `asdict` so that adding one is a visible act. `SELECTION_NOTICE`
// says what the order actually is: "the first person alphabetically who has
// confirmed it and is around … it is not an assessment of who is better suited,
// and there is no runner-up list."
//
// A frontend can undo all of that by accident. Sorting the group by how many
// skills each person covers, or by availability with the free people first,
// manufactures the ranking the payload refuses to carry — and it would look like
// a helpful default rather than a claim. So:
//
//   * `orderedGroup` sorts ALPHABETICALLY, always, with no option.
//   * There is deliberately no `coverageCount(member)` helper here, and none of
//     the per-person renderers below take one. "Covers 3 of 4" is a per-person
//     score with a different name.
//
// Counting the GROUP is a different thing and is fine: how many of the skills
// asked for are covered is a property of the answer, not of a person.

import type { TeamAnswerResponse, TeamAvailability, TeamMember } from '../../../api/smartTeams';

/** The group, alphabetically by display name (address as the tie-break so the
 *  order is stable across renders). Never by coverage, never by availability,
 *  never by anything derived from what a person holds. */
export function orderedGroup(group: TeamMember[]): TeamMember[] {
    return [...group].sort(
        (a, b) =>
            (a.name || a.email).localeCompare(b.name || b.email) ||
            a.email.localeCompare(b.email),
    );
}

/** GROUP-level coverage counts, for the "what the ask asked for" summary.
 *
 *  Both numbers are about the ask, not about anybody in the group. */
export function coverageTally(answer: TeamAnswerResponse): {
    required: number;
    requiredCovered: number;
    optional: number;
    optionalCovered: number;
    uncoveredRequired: { skill: string; name: string }[];
} {
    const required = answer.coverage.filter(item => item.required);
    const optional = answer.coverage.filter(item => !item.required);
    return {
        required: required.length,
        requiredCovered: required.filter(item => item.covered).length,
        optional: optional.length,
        optionalCovered: optional.filter(item => item.covered).length,
        uncoveredRequired: required
            .filter(item => !item.covered)
            .map(item => ({ skill: item.skill, name: item.name })),
    };
}

/** How the whole group's time off reads, as GROUP counts.
 *
 *  `AVAILABILITY_UNKNOWN` is counted separately and never folded into
 *  "available" — `AVAILABILITY_NOTICE` exists because "we did not check" must
 *  not render as "they are free". */
export function availabilityTally(group: TeamMember[]): Record<TeamAvailability, number> {
    const tally: Record<TeamAvailability, number> = {
        available: 0,
        out_part_of_window: 0,
        out_for_window: 0,
        unknown: 0,
    };
    for (const member of group) {
        if (member.availability in tally) tally[member.availability] += 1;
    }
    return tally;
}

/** Presentation labels for `team_contracts.AVAILABILITY_STATES`.
 *
 *  A state, spelled out. Never a day count: "out 6 days" is a number that sorts
 *  people, which is why the API sends a state plus a `back_on` date instead. */
export const AVAILABILITY_LABELS: Record<TeamAvailability, string> = {
    available: 'No time off booked in this window',
    out_part_of_window: 'Booked off for part of this window',
    out_for_window: 'Booked off for the whole window',
    unknown: 'No time-off record — not the same as free',
};
