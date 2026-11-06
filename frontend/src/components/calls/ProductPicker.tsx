// Which products this call is about.
//
// WHY THIS CONTROL EXISTS AT ALL. The failure it targets: AEs not splitting
// leads into multiple products, or not capturing required data during calls.
// Both halves are one problem — the AE runs one conversation that covers two
// of the company's products, files it as one thing, and the second product
// quietly never becomes a lead. So this is a MULTI-select, and everything
// downstream keeps the products apart: one panel section each, one coverage
// list each, one lead each.
//
// FOUR THINGS HERE ARE DELIBERATE:
//
// 1. IT IS MULTI-SELECT, NOT A DROPDOWN. A single-choice control would encode
//    exactly the mistake this feature exists to correct, and it would do it
//    invisibly — the AE would pick the biggest product and the call would look
//    correctly scripted.
// 2. A REMEMBERED PICK WITH NO LIVE CHECKLIST STILL RENDERS, AND STILL COUNTS.
//    `selected` is what the AE chose last time with this client; `available` is
//    what Cubie can show a checklist for. They are different facts. A product
//    whose script was retired (or was never approved) must not silently unpick
//    itself — that would change the AE's answer to "what is this call about?"
//    without telling them, and it would drop a lead.
// 3. NOTHING HERE BLOCKS CAPTURE. Most products will have no script on day one
//    — the tables ship EMPTY because the content is the product owners' to
//    author and approve. A row that refused to start a call because nobody
//    has written a checklist yet would be a worse failure than the one this
//    fixes, so the empty catalogue says so in a sentence and gets out of the way.
// 4. THE PICK IS FROZEN ONCE THE CALL IS CAPTURED, because there is nothing
//    left to send it with: the row stops offering Capture, and a control that
//    still moves but changes nothing is a lie about what Cubie knows.
import React from 'react';
import { Package, PackageOpen } from 'lucide-react';
import { productKeyLabel, type ScriptOption } from '../../api/calls';

// Full literal class strings — an interpolated fragment is purged by the JIT.
const CHIP_ON =
    'flex cursor-pointer items-start gap-2.5 rounded-md border-2 border-brand bg-brand-tint px-3 py-2 transition-colors';
const CHIP_OFF =
    'flex cursor-pointer items-start gap-2.5 rounded-md border border-gray-300 bg-white px-3 py-2 transition-colors hover:border-brand hover:bg-brand-tint';
const CHIP_STATIC =
    'inline-flex items-center gap-1.5 rounded-full border border-gray-300 bg-gray-50 px-2.5 py-1 text-xs font-medium text-gray-700';

/** One row of the picker: an authored product, or a remembered pick whose
 *  script is gone. `has_script` is the difference and it is never hidden. */
interface Choice {
    product_key: string;
    name: string;
    item_count: number;
    has_script: boolean;
}

export interface ProductPickerProps {
    /** Unique per row: several pickers render at once, and two inputs sharing
     *  an id break the label association for both. */
    idPrefix: string;
    /** Every product with a live (approved) checklist. */
    available: readonly ScriptOption[];
    /** The AE's current pick, in pick order. */
    selected: readonly string[];
    onChange: (productKeys: string[]) => void;
    /** The call is already captured — the pick has been sent and cannot change. */
    frozen: boolean;
    /** Frozen rows only, and it decides a CLAIM. True when this browser sent the
     *  pick (captured during this visit), so "this call is about X" is a fact.
     *  False when the session predates this visit: `selected` is then the AE's
     *  REMEMBERED pick for this client, which is a good guess at what was
     *  attached and not a record of it — two calls with the same client on one
     *  day, scripted differently, would both show the later pick. The checklist
     *  is still worth reading; the sentence above it must not overclaim. */
    pickSent: boolean;
    /** The selection is still the server's remembered default, untouched by the
     *  AE this visit AND still against the company the server suggested. Only
     *  then is "remembered from last time" a true sentence. */
    remembered: boolean;
}

export const ProductPicker: React.FC<ProductPickerProps> = ({
    idPrefix,
    available,
    selected,
    onChange,
    frozen,
    pickSent,
    remembered,
}) => {
    const chosen = new Set(selected);

    // Note 2 — the union, not the intersection. Authored products first, in the
    // order the server sorted them; then any remembered pick Cubie can no longer
    // show a checklist for, so the AE can see it and unpick it deliberately.
    const known = new Set(available.map(o => o.product_key));
    const choices: Choice[] = [
        ...available.map(o => ({
            product_key: o.product_key,
            name: o.name || productKeyLabel(o.product_key),
            item_count: o.item_count,
            has_script: true,
        })),
        ...selected
            .filter(key => !known.has(key))
            .map(key => ({
                product_key: key,
                name: productKeyLabel(key),
                item_count: 0,
                has_script: false,
            })),
    ];

    // Append on add, filter on remove: the order is the AE's own pick order and
    // the panel renders its sections in it.
    const toggle = (key: string) => {
        onChange(chosen.has(key) ? selected.filter(k => k !== key) : [...selected, key]);
    };

    if (frozen) {
        // Note 4. Nothing to send, so nothing to change — but the products stay
        // on screen, because the checklist below is still what the AE is working
        // through while the call runs.
        if (selected.length === 0) return null;
        return (
            <div className="mt-3">
                <p className="text-xs font-medium text-gray-500">
                    {pickSent
                        ? 'This call is about'
                        : 'Usually covered with this client'}
                </p>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {choices
                        .filter(c => chosen.has(c.product_key))
                        .map(c => (
                            <span key={c.product_key} className={CHIP_STATIC}>
                                <Package size={12} className="text-brand" />
                                {c.name}
                            </span>
                        ))}
                </div>
            </div>
        );
    }

    // Note 3 — nothing authored yet, and nothing selected. The normal early
    // state of a system that ships empty on purpose. Say it once, plainly, and
    // do not imply anything is broken or that Capture is affected.
    if (choices.length === 0) {
        return (
            <div className="mt-3 flex items-start gap-2 rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
                <PackageOpen size={14} className="mt-0.5 flex-shrink-0 text-gray-400" />
                <p className="text-xs leading-relaxed text-gray-600">
                    No call checklists have been published yet, so Cubie has nothing to prompt
                    you with on this call. Capture works exactly the same — run the call the way
                    you do today.
                </p>
            </div>
        );
    }

    return (
        <fieldset className="mt-3">
            <legend className="text-sm font-medium text-gray-900">
                What is this call about?
            </legend>
            <p className="mt-0.5 text-xs leading-relaxed text-gray-600">
                {remembered && selected.length > 0
                    ? 'Remembered from your last call with this client. Pick every product this ' +
                      'call will cover — each one gets its own checklist, and each one is its ' +
                      'own lead.'
                    : 'Pick every product this call will cover — each one gets its own ' +
                      'checklist, and each one is its own lead.'}
            </p>

            <div className="mt-2 flex flex-wrap gap-2">
                {choices.map(choice => {
                    const on = chosen.has(choice.product_key);
                    return (
                        <label
                            key={choice.product_key}
                            htmlFor={`${idPrefix}-${choice.product_key}`}
                            className={on ? CHIP_ON : CHIP_OFF}
                        >
                            <input
                                id={`${idPrefix}-${choice.product_key}`}
                                type="checkbox"
                                checked={on}
                                onChange={() => toggle(choice.product_key)}
                                className="mt-0.5 h-4 w-4 flex-shrink-0 rounded border-gray-300 text-brand focus:ring-brand"
                            />
                            <span className="min-w-0">
                                <span className="block text-sm font-medium text-gray-900">
                                    {choice.name}
                                </span>
                                <span className="block text-[11px] text-gray-500">
                                    {choice.has_script
                                        ? `${choice.item_count} ${
                                              choice.item_count === 1 ? 'fact' : 'facts'
                                          } to cover`
                                        : 'no checklist yet'}
                                </span>
                            </span>
                        </label>
                    );
                })}
            </div>

            {selected.length === 0 && (
                <p className="mt-2 text-xs text-gray-500">
                    Pick a product to see what this call has to cover. You can capture the call
                    without picking one — you just will not get a checklist.
                </p>
            )}
        </fieldset>
    );
};

export default ProductPicker;
