import React from 'react';
import { AlertTriangle, ArrowUpRight, Clock } from 'lucide-react';
import { isAgeState } from '../../api/cockpit';
import type { AgeState } from '../../api/cockpit';
import { Tooltip } from '../Tooltip';

// The aging chip that carries the escalation ladder onto every worklist row.
//
//   neutral    untracked    "3d since the call"      no lateness claimed
//   dashed     unknown      "age unknown"            no usable start date
//   grey       open         "2d"
//   amber      at_risk      "5d · SLA 7d"
//   red        breached     "9d · 2d over"
//   red + name escalated    "9d · 2d over · with Jordan"
//   red        exec_visible "12d · 5d over"          breach + 5 business days
//
// SEVEN STATES, NOT FOUR. `cockpit.py::AGE_STATES` is the closed set and the
// module asserts at import time that nothing else can reach a chip. The three
// beyond the ladder's four colours exist because the honest answer is
// sometimes not one of the four — and this component previously knew only four,
// so an `untracked` row ("3d since the call") and an `exec_visible` breach both
// rendered as the dashed "age unknown" chip. An exec-visible breach shown as a
// neutral shrug is the exact failure this feature exists to prevent.
//
// THREE THINGS IN HERE ARE LOAD-BEARING:
//
// 1. COLOUR IS NEVER THE ONLY SIGNAL. The number always renders next to it, and
//    breached/escalated also carry a glyph. That is accessibility, and it is
//    also what makes a screenshot pasted into Slack — how these actually get
//    discussed — still say what it says.
// 2. `label` is SERVER-RENDERED and printed verbatim. Elapsed time is
//    business-hours arithmetic through one backend module
//    (`rubik_core/cockpit/business_hours.py`); recomputing "6 days" here with
//    Date.now() produces the Friday-afternoon false positive that kills trust
//    in week one.
// 3. An UNKNOWN age renders as "age unknown", never as "0d". `Commitment.
//    opened_at` is nullable — a backfilled commitment with no
//    usable start date genuinely has no age, and printing zero hides exactly
//    the stall this feature exists to catch.
//
// A row with NO clock at all (a lead with no SLA yet) must render NO chip: the
// caller passes `age={undefined}` and gets nothing. Do not substitute a grey
// "open" chip there — that claims a clock exists.

/** Copy is fixed: an unagreed threshold has to say so. */
export const PLACEHOLDER_SLA_TOOLTIP = 'Placeholder — not yet confirmed by an admin.';

export interface AgeChipProps {
    /** The server's `age.state`. Typed loosely on purpose: an unrecognised
     *  value from a newer backend degrades to the unknown chip instead of
     *  indexing a class Record with `undefined` and rendering unstyled. */
    state?: AgeState | string | null;
    /** The server's `age.label`, printed verbatim. Blank / "unknown" → the
     *  unknown chip. */
    label?: string | null;
    /** Named on an escalated chip — "with Jordan". Escalation changes who is
     *  accountable, so naming the person IS the information (Rule 4). */
    escalatedTo?: string | null;
    /** From `CockpitItem.sla_is_placeholder`. Dots-underlines the SLA portion
     *  of the label and explains itself on hover/focus. */
    slaIsPlaceholder?: boolean;
}

// Full literal class strings, keyed by state. Never interpolate a Tailwind
// fragment — the JIT purges what it cannot see whole.
const CHIP_CLASSES: Record<AgeState, string> = {
    // No commitment governs the row, so there is no SLA to be inside or past.
    // Quieter than `open`: elapsed time, no claim of lateness.
    untracked: 'bg-white text-gray-600 border-gray-300',
    // Handled by the dashed branch below; present so the Record stays total.
    unknown: 'bg-white text-gray-500 border-gray-300 border-dashed',
    open: 'bg-gray-100 text-gray-700 border-gray-300',
    at_risk: 'bg-amber-50 text-amber-800 border-amber-300',
    breached: 'bg-red-50 text-red-800 border-red-300',
    escalated: 'bg-red-100 text-red-900 border-red-400',
    // "Rendered like breached" (cockpit.py). Deliberately the loudest of the
    // three reds: it is breach + 5 business days and it is in the sales
    // director's count.
    exec_visible: 'bg-red-100 text-red-900 border-red-500',
};

/** Dashed rather than solid, so "we don't know" doesn't read as "it's fine". */
const UNKNOWN_CHIP_CLASSES = CHIP_CLASSES.unknown;

const CHIP_ICONS: Record<AgeState, React.ComponentType<{ size?: number; className?: string }>> = {
    untracked: Clock,
    unknown: Clock,
    open: Clock,
    at_risk: Clock,
    breached: AlertTriangle,
    escalated: ArrowUpRight,
    exec_visible: ArrowUpRight,
};

const BASE_CHIP =
    'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium';

/** The server's separator. Split on it so the SLA segment can be decorated on
 *  its own without reflowing anything the server wrote. */
const SEP = '·';

const isUnknownLabel = (label?: string | null): boolean => {
    const t = (label ?? '').trim();
    return t === '' || t.toLowerCase() === 'unknown' || t.toLowerCase() === 'age unknown';
};

export const AgeChip: React.FC<AgeChipProps> = ({ state, label, escalatedTo, slaIsPlaceholder }) => {
    // `isAgeState` is the narrowing guard from api/cockpit.ts. `unknown` IS one
    // of the seven server states now, so it is matched by name as well — and a
    // state this build does not recognise still lands here rather than indexing
    // the class Record with `undefined` and rendering unstyled.
    const unknown = !isAgeState(state) || state === 'unknown' || isUnknownLabel(label);

    if (unknown) {
        return (
            <span
                className={`${BASE_CHIP} ${UNKNOWN_CHIP_CLASSES}`}
                aria-label="Age unknown — no usable start date on record"
                title="No usable start date on record, so there is no age to show."
            >
                <Clock size={13} aria-hidden="true" />
                age unknown
            </span>
        );
    }

    const chipState = state as AgeState;
    const Icon = CHIP_ICONS[chipState];

    // Verbatim label, plus the escalation target when the server did not
    // already fold it in ("9d · 2d over" + "with Jordan").
    let text = (label ?? '').trim();
    const target = (escalatedTo ?? '').trim();
    // The router folds the target into the label for `escalated` only, but the
    // `escalated_to` column survives into `exec_visible` — so name it there too
    // rather than dropping the one fact that says who now owns the chase.
    const namesTarget = chipState === 'escalated' || chipState === 'exec_visible';
    if (namesTarget && target && !text.toLowerCase().includes(target.toLowerCase())) {
        text = `${text} ${SEP} with ${target}`;
    }

    const segments = text.split(SEP).map(s => s.trim()).filter(Boolean);
    const slaIndex = segments.findIndex(s => /sla/i.test(s));
    // Placeholder styling has to be visible even if the label never spells out
    // "SLA" — better the whole chip is marked provisional than the caveat is
    // lost.
    const decorateIndex = slaIsPlaceholder ? (slaIndex === -1 ? 0 : slaIndex) : -1;

    return (
        <span className={`${BASE_CHIP} ${CHIP_CLASSES[chipState]}`} aria-label={`Age: ${text}`}>
            <Icon size={13} aria-hidden="true" />
            <span>
                {segments.map((seg, i) => (
                    <React.Fragment key={`${i}-${seg}`}>
                        {i > 0 && <span aria-hidden="true"> {SEP} </span>}
                        {i === decorateIndex ? (
                            <Tooltip content={PLACEHOLDER_SLA_TOOLTIP}>
                                <span className="border-b border-dotted border-current">{seg}</span>
                            </Tooltip>
                        ) : (
                            seg
                        )}
                    </React.Fragment>
                ))}
            </span>
        </span>
    );
};
