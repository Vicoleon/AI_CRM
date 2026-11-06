// Barrel for the sales-cockpit worklist components, plus the two small adapters
// the pages share.
//
// WHY THE ADAPTERS LIVE HERE AND NOT NEXT TO THE COMPONENTS: eslint's
// `react-refresh/only-export-components` is an error, and it scans `.tsx` only
// (`allowConstantExport` covers a bare string const, nothing more). A `.tsx`
// that exports a helper FUNCTION fails lint. A `.ts` file is not scanned at
// all, so this barrel is the right home for the two functions both pages need —
// the alternative is duplicating them in MyDay and SupervisorWorklist and
// watching them drift.
import type { AgeChipProps } from './AgeChip';
import { MY_DAY_BLOCK_ORDER } from '../../api/cockpit';
import type { CockpitItem, MyDayBlockKey, NudgeOutcome, NudgeResult } from '../../api/cockpit';

export { AgeChip, PLACEHOLDER_SLA_TOOLTIP } from './AgeChip';
export type { AgeChipProps } from './AgeChip';

export { ActionRow } from './ActionRow';
export type { ActionRowAction, ActionRowProps } from './ActionRow';

export { StageLagCard } from './StageLagCard';
export type { StageLagApplyAction, StageLagCardProps, StageLagRejectAction } from './StageLagCard';

export { DeferDealModal } from './DeferDealModal';
export type { DeferDealModalProps } from './DeferDealModal';
export { DeferredDeals } from './DeferredDeals';

/** Anti-tsunami Rule 2: a hard cap of three actionable items per block, with
 *  the remainder shown as a count that LINKS to a fuller view. A list of
 *  twenty is a list of zero. The server caps too (`MAX_INAPP_ITEMS` in
 *  `rubik_core/cockpit/digest.py`); this is the client half of the same rule,
 *  so a backend that forgets cannot flood the page. */
export const MAX_INAPP_ITEMS = 3;

/** Build the age chip's props from a server item, or `undefined` when the row
 *  has no clock at all — a lead with no SLA yet gets NO chip rather than a grey
 *  "open" one that would claim a clock exists. */
export function ageChipPropsFor(item: CockpitItem): AgeChipProps | undefined {
    if (!item.age) return undefined;
    return {
        state: item.age.state,
        label: item.age.label,
        escalatedTo: item.age.escalated_to,
        slaIsPlaceholder: item.sla_is_placeholder,
    };
}

/** Narrow a `?block=` query param before using it to index the block Record. */
export function isMyDayBlockKey(value: string | null | undefined): value is MyDayBlockKey {
    return typeof value === 'string' && (MY_DAY_BLOCK_ORDER as readonly string[]).includes(value);
}

/** What actually went out, in the words of the response body.
 *
 *  A 200 DOES NOT PROVE A DM WAS SENT: `SlackBotClient.send_dm` returns None for
 *  a non-member and the cockpit counts that as a skip, not a failure — the
 *  same trap that had an existing Slack follow-up path reporting success while
 *  sending nothing. So a blind "Nudged" is banned
 *  here; we say which half landed.
 *
 *  READS `nudge.py::_result`'S ACTUAL FIELDS. `sent` means the HubSpot TASK
 *  landed — it says nothing about Slack. Slack has its own named state in
 *  `slack`. An earlier version of this function read a `slack_sent` boolean
 *  that the server has never sent, so every nudge reported "HubSpot task
 *  created." and the skip was invisible — which is the precise bug the
 *  skip-not-failure rule exists to prevent. */
export function nudgeOutcomeMessage(result: NudgeResult): string {
    const n: NudgeOutcome = result.nudge ?? {};

    // ── operational refusals: a 200, sent: false, and a named reason ─────────
    // Each is actionable and each is a different sentence. A failed EXTERNAL
    // write is a 502 instead, so it never reaches here.
    if (n.sent === false) {
        switch (n.reason) {
            case 'escalation_required': {
                const target = (n.escalated_to ?? '').trim();
                const count = n.unacknowledged_nudges;
                const chased = typeof count === 'number' ? `${count} nudges` : 'Two nudges';
                return target
                    ? `Not nudged. ${chased} are already unacknowledged, so this needs escalating to ${target} — a third nudge is not the next step.`
                    : `Not nudged. ${chased} are already unacknowledged, so this needs escalating rather than chasing again.`;
            }
            case 'already_closed':
                return 'Not nudged — that commitment is already closed, so there is nothing to chase.';
            case 'no_crm_record':
                return 'Not nudged — there is no HubSpot deal or company on this row yet, so a task would attach to nothing. Add the CRM record first; the clock keeps running either way.';
            default:
                return n.reason
                    ? `Not nudged (${n.reason}).`
                    : 'Not nudged. The server did not say why — check the row before chasing by hand.';
        }
    }

    // ── it went: say which halves ───────────────────────────────────────────
    const task = !!n.hubspot_task_id;
    const taskClause = task ? 'HubSpot task created' : 'Nudge logged';
    switch (n.slack) {
        case 'delivered':
            return `Nudged — ${taskClause} and the Slack DM was delivered.`;
        case 'not_a_slack_workspace_member': {
            const who = (n.to_email ?? '').trim();
            return `${taskClause}. Slack DM skipped — ${who || 'the AE'} is not in the Slack workspace. The nudge is still on the record.`;
        }
        case 'slack_bot_unavailable':
            return `${taskClause}. No Slack DM went out — the Slack bot is not configured in this environment.`;
        case 'failed':
            return `${taskClause}. The Slack DM FAILED${n.slack_error ? ` (${n.slack_error})` : ''} — tell them another way.`;
        default:
            break;
    }
    // No `slack` key at all. Fall back to the channel the row recorded rather
    // than implying a person was reached.
    if (n.channel === 'both') return `${taskClause} and a Slack DM was sent.`;
    if (n.channel === 'hubspot_task') return `${taskClause}. No Slack DM.`;
    if (n.sent === true) return `${taskClause}. The server did not report whether Slack was reached.`;
    return 'The server acknowledged the nudge but reported nothing about it. Check the AE’s HubSpot tasks before chasing again.';
}

/** Sent-and-unacknowledged nudges on this commitment after the write, or null
 *  when the server did not report one. Rendered on the row because "nudged
 *  twice, still open" is an ESCALATION TRIGGER rather than somebody's memory —
 *  the first nudge has to be visible for the second to mean anything. */
export function nudgeCountFrom(result: NudgeResult): number | null {
    const count = result.nudge?.unacknowledged_nudges;
    return typeof count === 'number' ? count : null;
}

/** True when the server refused because the ladder says escalate, not nudge
 *  again (Rule 4). The button must stop offering itself after this. */
export function nudgeNeedsEscalation(result: NudgeResult): boolean {
    return result.nudge?.sent === false && result.nudge?.reason === 'escalation_required';
}
