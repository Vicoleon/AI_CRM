/** Template picker on the Proposal Generator's brief form.
 *
 *  The AE picks which of the company's proposal templates the document
 *  carries (or none: the proposal is then the AI-written sections alone), and
 *  can add a template's catalog list price as a pricing line in one click.
 *  The AI suggests the template from the call; the AE's choice here is what
 *  prints. Admins write the templates in Admin → Company setup.
 *
 *  The template's write-up is the company's words and prints as written.
 *  Prices are never taken from it: a list-price chip adds a line with the
 *  catalog figure as the standard rate and a blank proposal price for the AE
 *  to set.
 */
import React from 'react';
import { Plus } from 'lucide-react';
import {
    chooseTemplate,
    pricingLineFromOption,
    type Brief,
    type ProposalTemplate,
} from '../../api/proposals';

const LABEL = 'block text-xs font-medium text-gray-600 mb-1';
const INPUT =
    'w-full rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-900 shadow-sm transition-colors hover:border-brand-soft focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20';

export const ProposalTemplatePicker: React.FC<{
    brief: Brief;
    templates: ProposalTemplate[] | null;
    onChange: (patch: Partial<Brief>) => void;
}> = ({ brief, templates, onChange }) => {
    const chosenKey = brief.template?.key ?? '';
    const tpl = templates?.find(t => t.key === chosenKey) ?? null;

    if (templates === null) {
        return <p className="text-xs text-gray-500">Loading the company’s templates…</p>;
    }
    if (templates.length === 0) {
        return (
            <p className="text-xs text-gray-500" data-tour="proposal-template">
                No proposal templates yet, so the proposal is written from the call alone. An admin can add them in
                Admin → Company setup.
            </p>
        );
    }

    const priced = new Set(brief.pricing.map(p => p.description.trim().toLowerCase()));

    return (
        <div className="space-y-3" data-tour="proposal-template">
            <div>
                <label className={LABEL} htmlFor="proposal_template">
                    Template
                </label>
                <select
                    id="proposal_template"
                    className={INPUT}
                    value={chosenKey}
                    onChange={e => onChange({ template: chooseTemplate(templates, e.target.value) })}
                >
                    <option value="">None (the AI-written sections only)</option>
                    {templates.map(t => (
                        <option key={t.key} value={t.key}>
                            {t.name}
                        </option>
                    ))}
                </select>
                <p className="mt-1 text-xs text-gray-500">
                    {tpl
                        ? `The document adds the ${tpl.name} write-up, word for word, after your scope of work.`
                        : 'Pick a template to add the company’s standard write-up of the service.'}
                </p>
            </div>

            {tpl && tpl.price_options.length > 0 && (
                <div className="rounded-md border border-gray-200 p-3">
                    <p className="mb-1.5 text-xs font-medium text-gray-600">
                        Add a list price as a pricing line (you set the proposal price)
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                        {tpl.price_options.map(o => {
                            const added = priced.has(o.description.trim().toLowerCase());
                            return (
                                <button
                                    key={o.description}
                                    type="button"
                                    disabled={added}
                                    onClick={() => onChange({ pricing: [...brief.pricing, pricingLineFromOption(o)] })}
                                    className="inline-flex items-center gap-1 rounded-full border border-gray-300 px-2.5 py-1 text-xs text-gray-700 transition-colors hover:border-brand hover:bg-brand-tint hover:text-brand-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 active:bg-brand-soft/60 disabled:cursor-default disabled:opacity-50 disabled:hover:border-gray-300 disabled:hover:bg-transparent disabled:hover:text-gray-700"
                                    title={added ? 'Already on the brief' : 'Add this pricing line'}
                                >
                                    <Plus size={12} />
                                    {o.description} · {o.standard_rate}
                                </button>
                            );
                        })}
                    </div>
                </div>
            )}
        </div>
    );
};
