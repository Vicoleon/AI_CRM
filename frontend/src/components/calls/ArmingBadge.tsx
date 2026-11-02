// The transcription verdict on one calendar row.
//
// Arming is the SECONDARY job of the calendar screen — the primary one is
// binding the meeting to the right HubSpot company. Three facts make
// arm-everything impossible: a conference already in progress cannot be armed,
// a client-hosted Meet space is in another Workspace and cannot be patched at
// all, and a recurring series reuses ONE space so arming this week arms every
// future occurrence.
//
// So this badge has one job and one prohibition. The job: never let an
// unarmable row look BROKEN — the company binding is what lets the wrap-up
// write to the right CRM record, and that half works regardless. The
// prohibition: never let an unarmable row look ARMED. Hence amber, not red, and
// never silence.
//
// `detail` is the SERVER's sentence, rendered verbatim. The one thing this
// component decides for itself is whether to render it at all: a tenant-wide
// reason (a Workspace edition without automatic transcription, or Cubie's own
// kill switch) is identical on every row, so the page states it once above the
// list and passes it down here to be suppressed. Thirty copies of the same
// paragraph reads as thirty problems.
import React from 'react';
import { CheckCircle2, CircleDashed, Info, TriangleAlert } from 'lucide-react';
import { isTenantWideArmingReason, type ArmingVerdict } from '../../api/calls';
import { armingCopy, type VerdictTone } from './verdictCopy';

// Full literal class strings — an interpolated fragment is purged by the JIT.
const TONE_BADGE: Record<VerdictTone, string> = {
    green: 'inline-flex items-center gap-1.5 rounded-full border border-green-200 bg-green-50 px-2.5 py-0.5 text-[11px] font-medium text-green-800',
    amber: 'inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800',
    blue: 'inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-2.5 py-0.5 text-[11px] font-medium text-blue-800',
    gray: 'inline-flex items-center gap-1.5 rounded-full border border-gray-200 bg-gray-100 px-2.5 py-0.5 text-[11px] font-medium text-gray-600',
};

const TONE_DETAIL: Record<VerdictTone, string> = {
    green: 'mt-1.5 text-xs leading-relaxed text-green-800',
    amber: 'mt-1.5 text-xs leading-relaxed text-amber-800',
    blue: 'mt-1.5 text-xs leading-relaxed text-blue-800',
    gray: 'mt-1.5 text-xs leading-relaxed text-gray-500',
};

const TONE_ICON: Record<VerdictTone, React.ReactNode> = {
    green: <CheckCircle2 size={12} />,
    amber: <TriangleAlert size={12} />,
    blue: <Info size={12} />,
    gray: <CircleDashed size={12} />,
};

export interface ArmingBadgeProps {
    verdict: ArmingVerdict | null | undefined;
    /** Whether this occurrence already has a `call_sessions` row. It is what
     *  disambiguates `unknown`/`not_checked_yet`: "nobody has asked Google yet"
     *  before a session exists, and "Cubie deliberately never asked" — the
     *  confidential case — after one does. */
    hasSession: boolean;
    /** True to render the server's `detail` sentence under the badge. Off for a
     *  reason the page has already stated once above the list. */
    showDetail?: boolean;
}

export const ArmingBadge: React.FC<ArmingBadgeProps> = ({
    verdict,
    hasSession,
    showDetail = true,
}) => {
    if (!verdict) return null;
    const copy = armingCopy(verdict, { hasSession });
    // A tenant-wide reason is stated once, above the list. The BADGE still
    // renders — the row must not look unexplained — only the paragraph is
    // suppressed.
    const detail =
        showDetail && !isTenantWideArmingReason(verdict.reason) ? verdict.detail : null;

    return (
        <div>
            <span className={TONE_BADGE[copy.tone]}>
                {TONE_ICON[copy.tone]}
                {copy.label}
            </span>
            {detail && <p className={TONE_DETAIL[copy.tone]}>{detail}</p>}
        </div>
    );
};

export default ArmingBadge;
