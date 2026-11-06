// What this call has to cover — on screen before the AE dials, and still on
// screen while they are talking.
//
// THE PROBLEM THIS WHOLE PANEL EXISTS FOR: whatever is not asked or not
// covered in the call is missed, no matter what automation runs afterwards,
// and the team is back where it started. A perfect extractor over an
// incomplete conversation still produces an incomplete HubSpot record, and the
// AE cannot tell, because the wrap-up looks clean. This is the only place in
// the flow where that is still fixable — before the question goes unasked.
//
// WHAT IT IS NOT. It is a STATIC CHECKLIST, not a live assistant. There is no
// supported way to get audio out of a Meet call with external participants, so
// nothing here updates during the call, nothing ticks itself, and Cubie is not
// listening; mid-call nudging is ruled out by design. The footnote says so in
// plain words rather than leaving the AE to infer it from silence — an AE who
// believes Cubie is following along stops watching the checklist, which is
// precisely the failure mode this replaces.
//
// FIVE THINGS HERE ARE DELIBERATE:
//
// 1. ONE SECTION PER PRODUCT. Never a merged list, never a blended count. The
//    whole reason the feature exists is that AEs fail to treat several products
//    on one call as several things (each product is its own lead), and a UI
//    that merges them teaches exactly that mistake.
// 2. A SELECTED PRODUCT WITH NO CHECKLIST GETS ITS OWN SECTION SAYING SO. It is
//    not dropped. "Nobody has written this one yet" and "this product is
//    not on this call" are different facts, and only the first is true here.
// 3. NOTHING IS BEHIND A DISCLOSURE. No accordion, no "show more", no state of
//    its own — the AE is talking to a client and cannot be asked to click. It
//    also means the panel cannot collapse or reorder underneath them.
// 4. IT SITS ABOVE THE ARMING BADGE ON THE ROW, on purpose. The transcription
//    verdict changes when the call is captured; anything rendered below it
//    jumps down the page at the exact moment the AE is joining the call.
// 5. REQUIRED IS THE DEFAULT AND OPTIONAL IS THE MARKED CASE — the same
//    asymmetry the server applies. A wrongly-required fact costs a second to
//    dismiss; a wrongly-optional one never shows a gap at all.
import React from 'react';
import { CircleDot, ClipboardList, Circle, PackageOpen } from 'lucide-react';
import { productKeyLabel, type ScriptSection } from '../../api/calls';

// Full literal class strings — an interpolated fragment is purged by the JIT.
const PANEL = 'mt-3 rounded-md border border-gray-200 bg-white p-3';
const SECTION = 'rounded-md border border-gray-200 bg-gray-50 p-3';
const SECTION_EMPTY = 'rounded-md border border-dashed border-gray-300 bg-white p-3';

export interface PreCallPanelProps {
    /** The AE's pick, in pick order — the order the sections render in. */
    selected: readonly string[];
    /** The checklists that resolved. A selected key with no section here has no
     *  approved checklist, and gets a section of its own that says that — so
     *  build this with `checklistsFor` against the day's catalogue, never from
     *  the row's remembered `sections` alone, or a fresh pick reads as having
     *  no checklist when it has one. */
    sections: readonly ScriptSection[];
    /** The row already has a `call_sessions` row. Changes the footnote only:
     *  the checklist itself is exactly as useful during the call as before it. */
    hasSession: boolean;
    /** This call is (or will be) captured confidentially — never transcribed,
     *  so no wrap-up will ever check any of this off. Said out loud, because an
     *  AE who expects the wrap-up to catch a gap will not catch it themselves. */
    confidential: boolean;
}

const factCount = (n: number): string => `${n} ${n === 1 ? 'fact' : 'facts'}`;

export const PreCallPanel: React.FC<PreCallPanelProps> = ({
    selected,
    sections,
    hasSession,
    confidential,
}) => {
    if (selected.length === 0) return null;

    const byKey = new Map(sections.map(s => [s.product_key, s]));

    return (
        <div className={PANEL}>
            <div className="flex items-center gap-2">
                <ClipboardList size={15} className="flex-shrink-0 text-brand" />
                <p className="text-sm font-semibold text-gray-900">
                    What to ask on this call
                </p>
                {selected.length > 1 && (
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                        {selected.length} products
                    </span>
                )}
            </div>

            {/* Note 1 — one section per product, in the AE's pick order. */}
            <div className="mt-2.5 space-y-2.5">
                {selected.map(key => {
                    const section = byKey.get(key);

                    // Note 2 — selected, but nobody has authored a checklist for
                    // it. Kept visible: the product is still on this call and
                    // still its own lead.
                    if (!section) {
                        return (
                            <div key={key} className={SECTION_EMPTY}>
                                <div className="flex items-start gap-2">
                                    <PackageOpen
                                        size={14}
                                        className="mt-0.5 flex-shrink-0 text-gray-400"
                                    />
                                    <div className="min-w-0">
                                        <p className="text-sm font-semibold text-gray-900">
                                            {productKeyLabel(key)}
                                        </p>
                                        <p className="mt-0.5 text-xs leading-relaxed text-gray-600">
                                            No approved checklist for this one yet, so Cubie has
                                            nothing to prompt you with — ask what you normally
                                            would. It still counts as its own product on this
                                            call.
                                        </p>
                                    </div>
                                </div>
                            </div>
                        );
                    }

                    const required = section.items.filter(i => i.required).length;
                    return (
                        <div key={key} className={SECTION}>
                            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                                <p className="text-sm font-semibold text-gray-900">
                                    {section.name || productKeyLabel(section.product_key)}
                                </p>
                                <p className="text-[11px] text-gray-500">
                                    {factCount(section.items.length)}
                                    {required > 0 && ` · ${required} required`}
                                </p>
                            </div>

                            {section.items.length === 0 ? (
                                <p className="mt-1.5 text-xs leading-relaxed text-gray-600">
                                    This checklist has no facts on it yet.
                                </p>
                            ) : (
                                // Author sequence, straight off the wire. Never
                                // re-sorted: the order is how the conversation
                                // is meant to run.
                                <ul className="mt-2 space-y-2">
                                    {section.items.map(item => (
                                        <li key={item.fact_key} className="flex items-start gap-2">
                                            {/* Note 5 — solid for required,
                                                hollow for optional, and the word
                                                as well as the mark. */}
                                            {item.required ? (
                                                <CircleDot
                                                    size={13}
                                                    className="mt-0.5 flex-shrink-0 text-brand"
                                                    aria-hidden="true"
                                                />
                                            ) : (
                                                <Circle
                                                    size={13}
                                                    className="mt-0.5 flex-shrink-0 text-gray-400"
                                                    aria-hidden="true"
                                                />
                                            )}
                                            <span className="min-w-0">
                                                <span
                                                    className={
                                                        item.required
                                                            ? 'text-sm font-medium leading-snug text-gray-900'
                                                            : 'text-sm leading-snug text-gray-600'
                                                    }
                                                >
                                                    {item.prompt_text || item.fact_key}
                                                </span>
                                                {!item.required && (
                                                    <span className="ml-1.5 text-[10px] font-medium uppercase tracking-wide text-gray-400">
                                                        optional
                                                    </span>
                                                )}
                                                {item.help_text && (
                                                    <span className="mt-0.5 block text-xs leading-relaxed text-gray-500">
                                                        {item.help_text}
                                                    </span>
                                                )}
                                            </span>
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </div>
                    );
                })}
            </div>

            {/* The honesty footnote. Cubie is NOT in the call — see the header
                comment. Never imply otherwise, not even by omission. */}
            <p className="mt-3 border-t border-gray-100 pt-2 text-[11px] leading-relaxed text-gray-500">
                Cubie is not listening to this call. This is a checklist to read — nothing on it
                ticks itself while you talk.{' '}
                {confidential
                    ? 'And this call is confidential: it is never transcribed and there is no ' +
                      'wrap-up, so nothing here will be checked off afterwards either.'
                    : hasSession
                      ? 'After the call, the wrap-up shows which of these the conversation ' +
                        'actually surfaced, product by product.'
                      : 'Capture the call and, once it ends, the wrap-up shows which of these ' +
                        'the conversation actually surfaced, product by product.'}
            </p>
        </div>
    );
};

export default PreCallPanel;
