import type { LooseEndsScorecard } from '../../api/looseEnds';

/** Keep EOS-owned scorecard controls out of the core Team view when EOS is off. */
export function scorecardControls(
    canAccessEos: boolean,
    scorecard: LooseEndsScorecard | null | undefined,
): { scorecard: LooseEndsScorecard | null; canSetup: boolean } {
    const visible = canAccessEos ? scorecard ?? null : null;
    return {
        scorecard: visible,
        canSetup: visible?.can_setup === true && visible.people_without_metric > 0,
    };
}
