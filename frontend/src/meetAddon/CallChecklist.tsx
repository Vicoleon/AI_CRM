// The checklist, as it renders in Google Meet's side panel.
//
// Same rules as the Today's Calls panel (`components/calls/PreCallPanel.tsx`),
// shaped for a ~320px column beside a live call:
//
// 1. ONE SECTION PER PRODUCT, never a merged list — a call about three products
//    is three leads, and a UI that blends them teaches the opposite.
// 2. A PICKED PRODUCT WITH NO CHECKLIST STILL GETS ITS SECTION, saying so.
//    "Nobody has written this one yet" and "not on this call" are different.
// 3. NOTHING IS BEHIND A DISCLOSURE. The AE is talking to a client; the panel
//    must not collapse or reorder under them.
// 4. TICKS ARE THE AE'S. They are a working aid kept in this panel. Cubie is not
//    listening, nothing ticks itself, and the footnote says so — an AE who
//    thinks Cubie is following along stops watching the list.
// 5. EVERY "WE COULD NOT LOOK" STATE HAS ITS OWN SENTENCE, and none of them
//    reads like "you have nothing to ask".
import React from 'react';
import {
    Building2,
    CheckCircle2,
    ClipboardList,
    ExternalLink,
    PackageOpen,
    RefreshCw,
    ShieldOff,
    TriangleAlert,
    X,
} from 'lucide-react';
import {
    panelProducts,
    pickableSections,
    sectionProgress,
    tickKey,
    type AddonContext,
    type LocalState,
    type PanelProduct,
} from './panelState';

// Full literal class strings — an interpolated fragment is purged by the JIT.
const NOTE_AMBER = 'rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900';
const NOTE_GRAY = 'rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-700';
const NOTE_RED = 'rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-800';
const SECTION = 'rounded-md border border-slate-200 bg-white p-3 shadow-sm';
const SECTION_EMPTY = 'rounded-md border border-dashed border-slate-300 bg-white p-3';

export interface CallChecklistProps {
    ctx: AddonContext;
    local: LocalState;
    refreshing: boolean;
    onRefresh: () => void;
    onToggle: (key: string) => void;
    onAdd: (productKey: string) => void;
    onRemove: (productKey: string) => void;
}

function Notices({ ctx, shownCount }: { ctx: AddonContext; shownCount: number }) {
    const notes: React.ReactNode[] = [];
    const captured = Boolean(ctx.call?.call_session_id);
    // A calendar outage only costs the panel its answer when nothing else
    // named the call: a session found on the code still says which call it is.
    if (!ctx.calendar_available && ctx.match === 'none') {
        notes.push(
            <p key="calendar" className={NOTE_AMBER}>
                Cubie could not read your calendar just now, so it cannot tell which client call
                this is. Pick the products below to see their checklists, or refresh in a moment.
            </p>,
        );
    } else if (ctx.calendar_available && ctx.match === 'none') {
        notes.push(
            <p key="unknown" className={NOTE_GRAY}>
                This meeting is not one of your client calls in Cubie today. Cubie knows a call
                when it is on your calendar with at least one guest from outside your company.
                Pick the products below to see their checklists.
            </p>,
        );
    }
    if (ctx.calls_sharing_code > 1 && ctx.call?.title) {
        notes.push(
            <p key="shared" className={NOTE_GRAY}>
                This Meet link is on {ctx.calls_sharing_code} of today&apos;s calls. Showing{' '}
                <span className="font-medium">{ctx.call.title}</span>.
            </p>,
        );
    }
    if (ctx.company?.state === 'uncertain' && !ctx.call?.call_session_id) {
        notes.push(
            <p key="company" className={NOTE_AMBER}>
                Cubie is not sure which company this call is with. Confirm it on Today&apos;s
                Calls in Cubie so the wrap-up files against the right HubSpot record.
            </p>,
        );
    }
    if (ctx.call?.confidential) {
        notes.push(
            <p key="confidential" className={`${NOTE_AMBER} flex items-start gap-1.5`}>
                <ShieldOff size={13} className="mt-0.5 flex-shrink-0" />
                <span>
                    Captured as confidential: never transcribed, so nothing here will be checked
                    off after the call either.
                </span>
            </p>,
        );
    }
    if (!ctx.catalogue_available) {
        notes.push(
            <p key="catalogue" className={NOTE_RED}>
                Cubie could not load the checklists right now. Refresh in a moment; until then,
                ask what you normally would.
            </p>,
        );
    } else if (ctx.catalogue.length === 0 && shownCount === 0) {
        notes.push(
            <p key="none-approved" className={NOTE_GRAY}>
                No product checklists have been approved yet. They are written and signed off by
                the people who own each product, so they arrive one product at a time.
            </p>,
        );
    } else if (ctx.match !== 'none' && ctx.selected.length === 0) {
        notes.push(
            captured ? (
                <p key="no-products" className={NOTE_GRAY}>
                    No products are attached to this call in Cubie. Add a checklist below to
                    read it here; the wrap-up will not check it.
                </p>
            ) : (
                <p key="no-products" className={NOTE_GRAY}>
                    No products are picked for this call yet. Add them below, or pick them on
                    Today&apos;s Calls in Cubie, where the pick is remembered for this client.
                </p>
            ),
        );
    }
    // The wrap-up grades ONLY what is attached to the call session. A captured
    // call showing the client's remembered pick instead (the ad-hoc form takes
    // no products) must say so, or the AE trusts a check that will never run.
    if (captured && ctx.products_source === 'remembered' && ctx.selected.length > 0) {
        notes.push(
            <p key="remembered" className={NOTE_AMBER}>
                These are the products you picked for this client last time. They are not
                attached to this call in Cubie, so the wrap-up will not check them.
            </p>,
        );
    }
    if (notes.length === 0) return null;
    return <div className="space-y-2">{notes}</div>;
}

function ProductSection({
    product,
    ticks,
    catalogueAvailable,
    onToggle,
    onRemove,
}: {
    product: PanelProduct;
    ticks: readonly string[];
    /** False when Cubie could not read the checklists at all — every section
     *  is then missing for that reason, not because nobody wrote one. */
    catalogueAvailable: boolean;
    onToggle: (key: string) => void;
    onRemove: (productKey: string) => void;
}) {
    const added = product.origin === 'added';
    const removeButton = added && (
        <button
            type="button"
            onClick={() => onRemove(product.product_key)}
            className="flex-shrink-0 rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            aria-label={`Remove ${product.name} from this panel`}
        >
            <X size={14} />
        </button>
    );

    // Note 2 — picked, but nobody has authored or approved its checklist.
    if (!product.section) {
        return (
            <section className={SECTION_EMPTY}>
                <div className="flex items-start gap-2">
                    <PackageOpen size={14} className="mt-0.5 flex-shrink-0 text-slate-400" />
                    <div className="min-w-0 flex-1">
                        <h2 className="text-sm font-semibold text-slate-900">{product.name}</h2>
                        <p className="mt-0.5 text-xs leading-relaxed text-slate-600">
                            {catalogueAvailable
                                ? 'No approved checklist for this one yet, so ask what you ' +
                                  'normally would. It still counts as its own product on this call.'
                                : 'Cubie could not load this checklist right now. Refresh in a ' +
                                  'moment; until then, ask what you normally would.'}
                        </p>
                    </div>
                    {removeButton}
                </div>
            </section>
        );
    }

    const section = product.section;
    const progress = sectionProgress(section, ticks);
    const ticked = new Set(ticks);
    return (
        <section className={SECTION}>
            <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                    <h2 className="text-sm font-semibold text-slate-900">{product.name}</h2>
                    <p className="text-[11px] text-slate-500">
                        {progress.asked} of {progress.total} asked
                        {progress.requiredLeft > 0 && ` · ${progress.requiredLeft} required left`}
                    </p>
                </div>
                {progress.total > 0 && progress.requiredLeft === 0 && (
                    <CheckCircle2 size={15} className="mt-0.5 flex-shrink-0 text-emerald-600" aria-label="Every required question ticked" />
                )}
                {removeButton}
            </div>
            {added && (
                <p className="mt-1.5 text-[11px] leading-snug text-amber-800">
                    Added here only. It is not attached to the call in Cubie, so the wrap-up will
                    not check it.
                </p>
            )}
            {section.items.length === 0 ? (
                <p className="mt-2 text-xs text-slate-600">This checklist has no questions on it yet.</p>
            ) : (
                // Author order, straight off the wire: it is how the conversation
                // is meant to run.
                <ul className="mt-2 space-y-1.5">
                    {section.items.map(item => {
                        const key = tickKey(section.product_key, item.fact_key);
                        const done = ticked.has(key);
                        const inputId = `cubie-tick-${key}`;
                        return (
                            <li key={item.fact_key}>
                                <label htmlFor={inputId} className="flex cursor-pointer items-start gap-2">
                                    <input
                                        id={inputId}
                                        type="checkbox"
                                        checked={done}
                                        onChange={() => onToggle(key)}
                                        className="mt-0.5 h-4 w-4 flex-shrink-0 rounded border-slate-300 text-brand focus:ring-brand"
                                    />
                                    <span className="min-w-0">
                                        <span
                                            className={
                                                done
                                                    ? 'text-sm leading-snug text-slate-400 line-through'
                                                    : item.required
                                                      ? 'text-sm font-medium leading-snug text-slate-900'
                                                      : 'text-sm leading-snug text-slate-600'
                                            }
                                        >
                                            {item.prompt_text}
                                        </span>
                                        {!item.required && (
                                            <span className="ml-1.5 text-[10px] font-medium uppercase tracking-wide text-slate-400">
                                                optional
                                            </span>
                                        )}
                                        {item.help_text && !done && (
                                            <span className="mt-0.5 block text-xs leading-relaxed text-slate-500">
                                                {item.help_text}
                                            </span>
                                        )}
                                    </span>
                                </label>
                            </li>
                        );
                    })}
                </ul>
            )}
        </section>
    );
}

export const CallChecklist: React.FC<CallChecklistProps> = ({
    ctx,
    local,
    refreshing,
    onRefresh,
    onToggle,
    onAdd,
    onRemove,
}) => {
    const products = panelProducts(ctx, local.added);
    const pickable = pickableSections(ctx, local.added);
    const call = ctx.call;
    const company = ctx.company;
    const sessionId = call?.call_session_id ?? null;
    const cubieLink = sessionId
        ? { href: `/calls/${encodeURIComponent(sessionId)}/wrapup`, label: 'Open the wrap-up in Cubie' }
        : { href: '/calls/start', label: 'Open Today’s Calls in Cubie' };

    return (
        <div className="flex min-h-screen flex-col bg-slate-50 text-slate-900">
            <header className="border-b border-slate-200 bg-white px-4 py-3">
                <div className="flex items-center gap-2">
                    <ClipboardList size={16} className="flex-shrink-0 text-brand" />
                    <p className="flex-1 text-sm font-semibold">What to ask on this call</p>
                    <button
                        type="button"
                        onClick={onRefresh}
                        disabled={refreshing}
                        className="rounded p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
                        aria-label="Refresh from Cubie"
                        title="Refresh from Cubie"
                    >
                        <RefreshCw size={14} className={refreshing ? 'animate-spin' : undefined} />
                    </button>
                </div>
                {(company?.name || call?.title) && (
                    <div className="mt-2 space-y-0.5">
                        {company?.name && (
                            <p className="flex items-center gap-1.5 text-sm font-medium text-slate-900">
                                <Building2 size={13} className="flex-shrink-0 text-slate-400" />
                                <span className="truncate">{company.name}</span>
                            </p>
                        )}
                        {company?.state === 'suggested' && (
                            <p className="text-[11px] text-slate-500">
                                Matched from the guests&apos; email, not yet confirmed.
                            </p>
                        )}
                        {call?.title && <p className="truncate text-xs text-slate-500">{call.title}</p>}
                        {call?.start_local && (
                            <p className="text-[11px] text-slate-400">{call.start_local}</p>
                        )}
                    </div>
                )}
            </header>

            <main className="flex-1 space-y-3 px-4 py-3">
                <Notices ctx={ctx} shownCount={products.length} />

                {products.map(product => (
                    <ProductSection
                        key={product.product_key}
                        product={product}
                        ticks={local.ticks}
                        catalogueAvailable={ctx.catalogue_available}
                        onToggle={onToggle}
                        onRemove={onRemove}
                    />
                ))}

                {pickable.length > 0 && (
                    <div>
                        <label htmlFor="cubie-add-product" className="sr-only">
                            Add a product&apos;s checklist
                        </label>
                        <select
                            id="cubie-add-product"
                            value=""
                            onChange={e => {
                                if (e.target.value) onAdd(e.target.value);
                            }}
                            className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-700 focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                        >
                            <option value="">
                                {products.length === 0 ? 'Pick a product…' : 'Add another product…'}
                            </option>
                            {pickable.map(section => (
                                <option key={section.product_key} value={section.product_key}>
                                    {section.name}
                                </option>
                            ))}
                        </select>
                    </div>
                )}
            </main>

            <footer className="border-t border-slate-200 bg-white px-4 py-3">
                {/* The honesty footnote — see note 4. */}
                <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-slate-500">
                    <TriangleAlert size={12} className="mt-0.5 flex-shrink-0" />
                    <span>
                        Cubie is not listening to this call. Ticks are yours and stay in this
                        panel.{' '}
                        {call?.confidential
                            ? 'This call is confidential, so there is no wrap-up.'
                            : sessionId
                              ? ctx.products_source === 'call'
                                  ? 'After the call, the wrap-up shows which of the attached products’ questions the conversation actually covered.'
                                  : 'The wrap-up only checks products attached to the call in Cubie, and none are attached to this one.'
                              : ctx.match !== 'none'
                                ? 'Capture the call on Today’s Calls and, once it ends, the wrap-up shows which of these the conversation covered.'
                                : ''}
                    </span>
                </p>
                <a
                    href={cubieLink.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
                >
                    <ExternalLink size={12} /> {cubieLink.label}
                </a>
            </footer>
        </div>
    );
};

export default CallChecklist;
