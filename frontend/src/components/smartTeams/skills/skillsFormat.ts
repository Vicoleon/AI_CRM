// Pure presentation helpers for the ST-8 skills screens.
//
// Split from `skillsShared.tsx` because that file exports components and this one
// does not — the repo's lint rule keeps the two apart so fast refresh works, the
// same split as `interviewFlow.ts` next to `shared.tsx`.
//
// Nothing here invents a vocabulary. The level strings, the cluster names and the
// evidence `kind` values are all the server's; only the capitalisation and the
// grouping are ours.
import { parseInstant } from '../../../utils/datetime.ts';
import type { SkillLevel } from '../../../api/smartTeams';

/** Presentation of the server's `SKILL_LEVELS`. A value outside the vocabulary
 *  renders verbatim rather than being dropped. */
const LEVEL_LABELS: Record<SkillLevel, string> = {
    familiar: 'Familiar',
    working: 'Working knowledge',
    strong: 'Strong',
    expert: 'Expert',
};

/** `null` is a real value the API accepts — "I do this, at no stated level" —
 *  and not the absence of an answer, so it gets words rather than an em dash. */
export const levelLabel = (level: SkillLevel | null): string =>
    level ? (LEVEL_LABELS[level] ?? level) : 'Level not stated';

/** ISO → a date a person reads. Null rather than "Invalid Date". */
export const shortDate = (iso: string | null | undefined): string | null => {
    const parsed = parseInstant(iso);
    if (!parsed) return null;
    return parsed.toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
    });
};

/** Group by taxonomy cluster. A cluster with no rows simply does not appear, and
 *  a row with no cluster lands in "Other" rather than vanishing. */
export function byCluster<T extends { cluster: string | null; name: string }>(
    skills: T[],
): [string, T[]][] {
    const groups = new Map<string, T[]>();
    for (const skill of skills) {
        const key = skill.cluster ?? 'Other';
        const list = groups.get(key);
        if (list) list.push(skill);
        else groups.set(key, [skill]);
    }
    return [...groups.entries()]
        .map(([cluster, rows]): [string, T[]] => [
            cluster,
            [...rows].sort((a, b) => a.name.localeCompare(b.name)),
        ])
        .sort((a, b) => a[0].localeCompare(b[0]));
}

/** `skill_inference.KIND_*`. An unrecognised kind is shown as it arrived — the
 *  sweep owns that vocabulary and may grow it, and a citation whose kind this
 *  file has not heard of is still a citation the person should see. */
export const evidenceKindLabel = (kind: string): string => {
    if (kind === 'meeting') return 'Meeting';
    if (kind === 'document') return 'Document you uploaded';
    return kind;
};
