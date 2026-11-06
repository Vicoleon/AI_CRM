// The HubSpot company typeahead for binding a call.
//
// WHY IT HAS ITS OWN ENDPOINT. Cubie had exactly one company search,
// `/api/xray/customers`, and it sits behind Customer X-Ray's gate — a different
// audience that moves for its own reasons (it was Systems-only for the whole
// soft launch, so it 403'd every non-admin Sales AE, which is most of Sales: the
// people this screen exists for got its worst version and nothing errored).
// `GET /api/calls/companies` runs the same `search_companies` behind
// `require_ccs`, this feature's own audience.
//
// THREE THINGS HERE ARE DELIBERATE:
//
// 1. `searching` is DERIVED from a settled result keyed by the needle that
//    answered it, never stored. A `setSearching(true)` inside an effect keyed on
//    the query is an ERROR under `react-hooks/set-state-in-effect`.
// 2. The manual-ID escape hatch is offered on TWO different facts, with two
//    different sentences: the endpoint refused us (403/404 — it will not fix
//    itself, so search stops being offered for the rest of the session), and
//    the search simply matched nothing. "Your account cannot use the lookup" and
//    "no company by that name" are not the same thing and only one of them is
//    worth telling an administrator about.
// 3. `onPick` fires on a deliberate click, never on a keystroke. The manual
//    field therefore has its own button: a picker that bound a company while the
//    AE was still typing its id is a silent wrong binding, which is the exact
//    failure this whole screen is shaped to avoid.
import React, { useEffect, useState } from 'react';
import { Building2, Loader2, Search } from 'lucide-react';
import {
    companyOptionLabel,
    searchBindableCompanies,
    type CompanyOption,
} from '../../api/calls';
import { Field, inputCls } from '../eos/Modal';

/** One settled search, keyed by the needle it answered — see note 1. */
interface Settled {
    needle: string;
    rows: CompanyOption[];
}

export interface CompanyPickerProps {
    /** Unique per instance: several rows can render a picker at once, and two
     *  inputs sharing an id break the label association for both. */
    inputId: string;
    label?: string;
    placeholder?: string;
    /** Fired once, on a click. Never on a keystroke. */
    onPick: (company: CompanyOption) => void;
    /** Optional cancel affordance — the matched row's "keep the suggestion". */
    onCancel?: () => void;
    cancelLabel?: string;
}

const MANUAL_HINT_DENIED =
    'Company search is not answering right now — paste the company’s HubSpot record ID ' +
    'from its URL instead. The call is bound and captured in exactly the same way.';

const MANUAL_HINT_NO_MATCH =
    'Nothing matched. Try a shorter fragment of the name, or paste the company’s HubSpot ' +
    'record ID from its URL.';

export const CompanyPicker: React.FC<CompanyPickerProps> = ({
    inputId,
    label = 'Company',
    placeholder = 'Search a HubSpot company…',
    onPick,
    onCancel,
    cancelLabel = 'Cancel',
}) => {
    const [query, setQuery] = useState('');
    const [settled, setSettled] = useState<Settled | null>(null);
    // Set only when the endpoint refuses us. A 403 will not fix itself, so once
    // refused we stay on the manual field for the rest of the session.
    const [denied, setDenied] = useState(false);
    const [manualId, setManualId] = useState('');

    const needle = query.trim();
    const tooShort = needle.length < 2; // derived, never stored
    const matched = settled && settled.needle === needle ? settled : null;
    const searching = !denied && !tooShort && matched === null;
    const rows = matched?.rows ?? [];
    const noMatch = !denied && !tooShort && !searching && rows.length === 0;

    useEffect(() => {
        if (denied || tooShort) return;
        let cancelled = false;
        const timer = window.setTimeout(() => {
            searchBindableCompanies(needle)
                .then(found => {
                    if (!cancelled) setSettled({ needle, rows: found });
                })
                .catch(e => {
                    if (cancelled) return;
                    const status = (e as { response?: { status?: number } })?.response?.status;
                    if (status === 403 || status === 404) setDenied(true);
                    setSettled({ needle, rows: [] });
                });
        }, 250);
        return () => {
            cancelled = true;
            window.clearTimeout(timer);
        };
    }, [needle, tooShort, denied]);

    const bindManual = () => {
        const id = manualId.trim();
        if (!id) return;
        // No name and no domain: the row will render the bare id, which is
        // honest — nobody looked this company up, the AE typed it.
        onPick({ hubspot_company_id: id, name: '', domain: '', client_status: '' });
    };

    return (
        <div>
            {!denied && (
                <>
                    <label
                        htmlFor={inputId}
                        className="mb-1.5 block text-sm font-medium text-gray-700"
                    >
                        {label}
                    </label>
                    <div className="relative">
                        <Search
                            size={16}
                            className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
                        />
                        <input
                            id={inputId}
                            type="text"
                            autoComplete="off"
                            value={query}
                            onChange={e => setQuery(e.target.value)}
                            placeholder={placeholder}
                            className={`${inputCls} pl-9`}
                        />
                        {searching && (
                            <Loader2
                                size={16}
                                className="absolute right-3 top-1/2 -translate-y-1/2 animate-spin text-gray-400"
                            />
                        )}
                    </div>
                </>
            )}

            {rows.length > 0 && (
                <ul className="mt-2 max-h-64 divide-y divide-gray-100 overflow-y-auto rounded-md border border-gray-200">
                    {rows.map(row => (
                        <li key={row.hubspot_company_id}>
                            <button
                                type="button"
                                onClick={() => onPick(row)}
                                className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-brand-tint"
                            >
                                <Building2 size={16} className="flex-shrink-0 text-gray-400" />
                                <span className="min-w-0 flex-1">
                                    <span className="block truncate text-sm font-medium text-gray-900">
                                        {companyOptionLabel(row)}
                                    </span>
                                    {(row.domain || row.client_status) && (
                                        <span className="block truncate text-xs text-gray-500">
                                            {[row.domain, row.client_status]
                                                .filter(Boolean)
                                                .join(' · ')}
                                        </span>
                                    )}
                                </span>
                            </button>
                        </li>
                    ))}
                </ul>
            )}

            {(denied || noMatch) && (
                <div className="mt-3">
                    <p className="mb-2 text-xs text-gray-500">
                        {denied ? MANUAL_HINT_DENIED : MANUAL_HINT_NO_MATCH}
                    </p>
                    <Field label="HubSpot company ID">
                        <input
                            type="text"
                            value={manualId}
                            onChange={e => setManualId(e.target.value)}
                            placeholder="e.g. 12345678901"
                            className={inputCls}
                        />
                    </Field>
                    <button
                        type="button"
                        onClick={bindManual}
                        disabled={!manualId.trim()}
                        className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                        <Building2 size={13} /> Use this company ID
                    </button>
                </div>
            )}

            {onCancel && (
                <button
                    type="button"
                    onClick={onCancel}
                    className="mt-2 text-xs font-medium text-gray-500 hover:text-gray-700 hover:underline"
                >
                    {cancelLabel}
                </button>
            )}
        </div>
    );
};

export default CompanyPicker;
