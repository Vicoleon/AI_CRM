// Call scripts — the authoring screen.
//
// Scripts are not hard-coded: you choose a product and the minimum
// information it requires, and then you self-serve.
//
// THIS SCREEN IS THE DIFFERENCE between a script system that survives and one
// that is accurate for a quarter and wrong forever after. A script is a
// per-product checklist of the FACTS a client call has to surface for a complete
// HubSpot record and a buildable SOW — a typical product needs about five of
// them (the scope, how many units, hours per unit, the rate, the start date)
// before a real SOW can be written. If adding one is an engineering ticket, the
// checklist stops matching what the company sells the first time a product
// changes, and nobody notices, because a wrap-up over a stale checklist still
// looks clean.
//
// So the screen is a real editor for the people who own the products, not an
// admin JSON box. It ships EMPTY: no product, question or fact key is seeded
// anywhere in this feature. Everything on it arrives from the product owners.
//
// SIX DECISIONS HERE, EACH BECAUSE THE ALTERNATIVE FAILS QUIETLY:
//
// 1. APPROVAL IS SPELLED OUT ON THE BUTTON, not in a tooltip. `approved_by` is
//    the single column that decides whether AEs see this checklist on live client
//    calls, and the person clicking it is a subject-matter expert, not an
//    engineer reading an API contract. A tooltip is a place consequences go to be
//    missed.
// 2. EDITING AN APPROVED SCRIPT SAYS "this makes version N+1" BEFORE the click,
//    on a banner, and again on the save button's own label. The router forks
//    rather than mutates — calls already running on the current version keep
//    running on it — and an author who believes they just edited the live script
//    will not approve the fork, so their change silently never ships.
// 3. THE STATE IS NAMED IN WORDS: Draft / Approved / Superseded / Retired. Three
//    columns (`is_active`, `approved_by`, `is_live`) answer one question, and
//    `scriptState()` in the API client is the only place this app answers it.
// 4. THE QUESTIONS A SAVED DRAFT ALREADY HOLDS CANNOT BE READ BACK. There is no
//    `GET /api/scripts/{id}`: the catalogue returns counts without items, and
//    `GET /api/scripts/{product_key}` returns the LIVE APPROVED version only. So
//    for a draft this screen did not just write, the editor says so plainly and
//    OMITS `items` from the save (the router keeps the current checklist for an
//    omitted `items`) — metadata edits stay safe, and replacing the questions is
//    an explicit, confirmed act. Sending `[]` "because we could not read them"
//    would wipe a checklist nobody could see.
// 5. VALIDATION MIRRORS THE ROUTER AND RUNS ON SAVE, NOT ON MOUNT. A blank new
//    script is not an error yet, so nothing is red until the author asks to save;
//    the fact-key rule is then shown with a working suggestion, because a key
//    that reaches the database wrong renders an empty panel or reports a
//    discussed fact as missing, and neither ever mentions the key.
// 6. `loading` IS DERIVED from a settled result keyed by the load it answered,
//    never stored. `setLoading(true)` inside an effect keyed on a param is an
//    error under `react-hooks/set-state-in-effect`. Saves refresh the catalogue
//    IN PLACE (same key) so the list re-derives `is_live` without the page
//    flashing back to a spinner under the author's hands.
import React, { useEffect, useMemo, useState } from 'react';
import {
    ArrowDown,
    ArrowUp,
    CheckCircle2,
    ClipboardCheck,
    FilePlus2,
    Info,
    ListChecks,
    Plus,
    RefreshCw,
    RotateCcw,
    Save,
    Trash2,
    TriangleAlert,
} from 'lucide-react';
import {
    approveScript,
    createScript,
    deactivateScript,
    fetchActiveScript,
    fetchScripts,
    foldKey,
    groupByProduct,
    isAuthorDenied,
    isForbidden,
    isNotEnabled,
    isScriptNotFound,
    keyProblem,
    MAX_HELP_CHARS,
    MAX_KEY_CHARS,
    MAX_NAME_CHARS,
    MAX_NOTES_CHARS,
    MAX_PROMPT_CHARS,
    MAX_SCRIPT_ITEMS,
    MAX_TARGET_CHARS,
    restoreScript,
    savingForks,
    SCRIPT_STATE_HINTS,
    SCRIPT_STATE_LABELS,
    scriptSourceLabel,
    scriptState,
    scriptsError,
    suggestKey,
    targetProblem,
    updateScript,
    type ScriptCatalogue,
    type ScriptDetail,
    type ScriptItem,
    type ScriptItemInput,
    type ScriptsError,
    type ScriptState,
    type ScriptSummary,
    type ScriptUpdate,
} from '../../api/scripts';
import { BackButton } from '../../components/BackButton';
import { confirmDialog } from '../../components/eos/ConfirmDialog';
import { toast } from '../../components/eos/Toast';
import { useAuth } from '../../context/AuthContext';
import { EmptyState, EosPageHeader, ErrorState, LoadingState, PageContainer } from '../eos/_shared';

// Full literal class strings — an interpolated Tailwind fragment is purged by
// the JIT and the rule that stops it is worth more than the repetition.
const CARD = 'rounded-lg border border-gray-200 bg-white p-4 shadow-sm';
const NOTICE_AMBER = 'rounded-lg border border-amber-300 bg-amber-50 p-4';
const NOTICE_BLUE = 'rounded-lg border border-blue-200 bg-blue-50 p-4';
const NOTICE_GRAY = 'rounded-lg border border-gray-200 bg-gray-50 p-4';
const INPUT =
    'w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-brand focus:ring-1 focus:ring-brand disabled:bg-gray-50 disabled:text-gray-500';
const INPUT_BAD =
    'w-full rounded-md border border-red-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-red-500 focus:ring-1 focus:ring-red-500';
const LABEL = 'block text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1';
const BTN_PRIMARY =
    'inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50';
const BTN_APPROVE =
    'inline-flex items-center gap-2 rounded-md bg-green-700 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-green-800 disabled:cursor-not-allowed disabled:opacity-50';
const BTN_GHOST =
    'inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-sm transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50';
const BTN_ICON =
    'rounded-md border border-gray-200 bg-white p-1.5 text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-800 disabled:cursor-not-allowed disabled:opacity-40';

const STATE_BADGE: Record<ScriptState, string> = {
    draft: 'inline-flex items-center rounded-full border border-gray-200 bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-700',
    approved: 'inline-flex items-center rounded-full border border-green-200 bg-green-50 px-2 py-0.5 text-[11px] font-medium text-green-800',
    superseded: 'inline-flex items-center rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-800',
    retired: 'inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-800',
};

/** What a script IS, for somebody who has never seen one. Kept as copy rather
 *  than as an example script: seeding a product, a question or a fact key here
 *  would be an engineer authoring the product owners' content, which is the
 *  exact thing this feature exists to stop. The example facts are named as
 *  the SHAPE of an answer, not as a template to accept. */
const WHAT_IS_A_SCRIPT =
    'A script is the list of facts a call has to surface for this product — the things that, if nobody asks them, leave the CRM record and the SOW incomplete no matter how good the transcript is. A typical product needs about five of them (the scope, how many units, hours per unit, the rate, the start date). Write the questions the way an AE would say them out loud.';

// ─── the form model ──────────────────────────────────────────────────────────

/** One question being edited. `uid` is a React key only: the server assigns
 *  `sequence` from array order and this value is never sent. */
interface ItemDraft {
    uid: string;
    fact_key: string;
    prompt_text: string;
    hubspot_target: string;
    required: boolean;
    help_text: string;
}

interface FormState {
    /** null = a new script that has never been saved. */
    scriptId: string | null;
    productKey: string;
    name: string;
    hubspotProductId: string;
    notes: string;
    /** null = this screen does not hold the questions (decision 4). NOT the same
     *  as `[]`, which is a checklist an author has emptied on purpose. */
    items: ItemDraft[] | null;
    /** Snapshot at load, for dirtiness. */
    baseline: string;
}

let uidSeq = 0;
const nextUid = (): string => `i${++uidSeq}`;

const blankItem = (): ItemDraft => ({
    uid: nextUid(),
    fact_key: '',
    prompt_text: '',
    hubspot_target: '',
    required: true,
    help_text: '',
});

const toDraft = (item: ScriptItem): ItemDraft => ({
    uid: nextUid(),
    fact_key: item.fact_key ?? '',
    prompt_text: item.prompt_text ?? '',
    hubspot_target: item.hubspot_target ?? '',
    required: item.required !== false,
    help_text: item.help_text ?? '',
});

/** Everything that would be SENT, and nothing else — `uid` is excluded so
 *  reordering shows as a change and re-keying does not. */
const snapshot = (f: Omit<FormState, 'baseline'>): string =>
    JSON.stringify({
        productKey: foldKey(f.productKey),
        name: f.name.trim(),
        hubspotProductId: f.hubspotProductId.trim(),
        notes: f.notes.trim(),
        items:
            f.items?.map(i => [foldKey(i.fact_key), i.prompt_text.trim(), foldKey(i.hubspot_target), i.required, i.help_text.trim()]) ??
            null,
    });

const withBaseline = (f: Omit<FormState, 'baseline'>): FormState => ({ ...f, baseline: snapshot(f) });

const toInput = (items: ItemDraft[]): ScriptItemInput[] =>
    items.map(i => ({
        fact_key: foldKey(i.fact_key),
        prompt_text: i.prompt_text.trim(),
        hubspot_target: foldKey(i.hubspot_target) || null,
        required: i.required,
        help_text: i.help_text.trim() || null,
    }));

/** One settled catalogue load, keyed by the attempt it answered — decision 6. */
interface Loaded {
    key: number;
    catalogue: ScriptCatalogue | null;
    error: ScriptsError | null;
}

export const ScriptEditor: React.FC = () => {
    // The surface's own audience, mirroring `auth.user_can_access_ccs`. Call
    // scripts are a Call Capture surface: the checklist decides what the company
    // asks a client on a recorded call, so it follows the people who host those
    // calls and support the tooling. Whether this person may AUTHOR is a second,
    // narrower question the SERVER answers with `can_author` — never guessed here.
    const { canAccessCcs } = useAuth();

    const [reloadKey, setReloadKey] = useState(0);
    const [loaded, setLoaded] = useState<Loaded | null>(null);
    const [form, setForm] = useState<FormState | null>(null);
    /** Items keyed by script id, from every response that carried them. The only
     *  way this screen ever holds a version's questions — see decision 4. */
    const [itemCache, setItemCache] = useState<Record<string, ScriptItem[]>>({});
    const [opening, setOpening] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [showProblems, setShowProblems] = useState(false);

    const settled = loaded && loaded.key === reloadKey ? loaded : null;
    const loading = settled === null; // derived, never stored
    const catalogue = settled?.catalogue ?? null;
    const loadError = settled?.error ?? null;

    useEffect(() => {
        let cancelled = false;
        // Two reads, in one settle. The plain catalogue is allowed for everyone
        // the read gate admits and carries the server's own `can_author`; the
        // draft-bearing one 403s for anyone else, so asking for it first would
        // turn a legitimate read-only visit into an error page.
        (async () => {
            try {
                const live = await fetchScripts();
                const full = live.can_author ? await fetchScripts({ includeDrafts: true }) : live;
                if (!cancelled) setLoaded({ key: reloadKey, catalogue: full, error: null });
            } catch (e) {
                if (!cancelled) {
                    setLoaded({ key: reloadKey, catalogue: null, error: scriptsError(e, 'Call scripts could not be loaded.') });
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [reloadKey]);

    const scripts = useMemo(() => catalogue?.scripts ?? [], [catalogue]);
    const groups = useMemo(() => groupByProduct(scripts), [scripts]);
    const canAuthor = catalogue?.can_author === true;

    // Pulled out of `form` rather than read inside the memos below: an optional
    // member expression in a dependency array cannot be checked statically.
    const selectedId = form?.scriptId ?? null;
    const formProductKey = form?.productKey ?? '';
    /** null while nothing is open, AND when the open version's questions are not
     *  readable here (decision 4). The section below keeps those two apart. */
    const items: ItemDraft[] | null = form?.items ?? null;

    const selected = useMemo(
        () => (selectedId ? (scripts.find(s => s.script_id === selectedId) ?? null) : null),
        [scripts, selectedId],
    );

    const dirty = form ? snapshot(form) !== form.baseline : false;
    const isNew = form !== null && form.scriptId === null;

    /** The version a save would MINT when the selected script is approved. Read
     *  off the catalogue rather than `version + 1`: a product can already have a
     *  higher unapproved version, and the router assigns `max(version) + 1`. */
    const nextVersion = useMemo(() => {
        const key = foldKey(formProductKey);
        if (!key) return 1;
        const versions = scripts.filter(s => s.product_key === key).map(s => s.version);
        return versions.length ? Math.max(...versions) + 1 : 1;
    }, [scripts, formProductKey]);

    // ─── validation, mirroring the router (decision 5) ───────────────────────
    const problems = useMemo((): string[] => {
        if (!form) return [];
        const out: string[] = [];
        if (isNew) {
            const bad = keyProblem(form.productKey, 'Product key');
            if (bad) out.push(bad);
        }
        if (!form.name.trim()) out.push('A script needs a name.');
        const drafts = form.items;
        if (drafts) {
            if (drafts.length > MAX_SCRIPT_ITEMS) out.push(`A script can hold at most ${MAX_SCRIPT_ITEMS} questions.`);
            const seen = new Set<string>();
            drafts.forEach((item, index) => {
                const bad = keyProblem(item.fact_key, `Question ${index + 1}: fact key`);
                if (bad) out.push(bad);
                const key = foldKey(item.fact_key);
                if (key && seen.has(key)) out.push(`Fact key “${key}” is used by two questions — coverage cannot tell them apart.`);
                if (key) seen.add(key);
                if (!item.prompt_text.trim()) out.push(`Question ${index + 1} needs a question the AE can read out loud.`);
                const badTarget = targetProblem(item.hubspot_target);
                if (badTarget) out.push(`Question ${index + 1}: ${badTarget}`);
            });
        }
        // Deduped: one fact key repeated three times says the same sentence
        // three times, and the list is rendered keyed by the sentence.
        return [...new Set(out)];
    }, [form, isNew]);

    // ─── opening a version ───────────────────────────────────────────────────

    const openNew = (productKey = '', name = ''): void => {
        setShowProblems(false);
        setForm(
            withBaseline({
                scriptId: null,
                productKey,
                name,
                hubspotProductId: '',
                notes: '',
                items: [blankItem()],
            }),
        );
    };

    const openScript = async (summary: ScriptSummary): Promise<void> => {
        if (dirty && form) {
            const ok = await confirmDialog({
                title: 'Discard your unsaved changes?',
                message: 'This version has edits that have not been saved. Opening another one loses them.',
                confirmLabel: 'Discard and open',
                destructive: true,
            });
            if (!ok) return;
        }
        setShowProblems(false);

        let items = itemCache[summary.script_id] ?? null;
        // The one read that can recover a version's questions from the server:
        // `GET /api/scripts/{product_key}` returns the LIVE approved script, so
        // it answers only for the version that is live right now.
        if (!items && summary.is_live) {
            setOpening(summary.script_id);
            try {
                const detail = await fetchActiveScript(summary.product_key);
                // Guard the race: if a different version became live between the
                // catalogue load and this click, the items belong to THAT one and
                // attaching them here would put another version's questions under
                // this row's id.
                if (detail.script_id === summary.script_id) {
                    items = detail.items ?? [];
                    setItemCache(prev => ({ ...prev, [detail.script_id]: detail.items ?? [] }));
                }
            } catch (e) {
                const err = scriptsError(e, 'That script could not be opened.');
                // Not fatal: the version still opens, without its questions, and
                // the panel below says so rather than showing an empty checklist.
                if (!isScriptNotFound(err)) toast.error(err.message);
            } finally {
                setOpening(null);
            }
        }

        setForm(
            withBaseline({
                scriptId: summary.script_id,
                productKey: summary.product_key,
                name: summary.name ?? '',
                hubspotProductId: summary.hubspot_product_id ?? '',
                notes: summary.notes ?? '',
                items: items ? items.map(toDraft) : null,
            }),
        );
    };

    // ─── writes ──────────────────────────────────────────────────────────────

    /** Re-read the catalogue IN PLACE. Same key, so `settled` stays truthy and
     *  the page does not flash back to a spinner (decision 6). Every write can
     *  change which row is live, so the list is re-derived rather than patched. */
    const refresh = async (): Promise<ScriptCatalogue | null> => {
        try {
            const live = await fetchScripts();
            const full = live.can_author ? await fetchScripts({ includeDrafts: true }) : live;
            setLoaded({ key: reloadKey, catalogue: full, error: null });
            return full;
        } catch {
            // The write already succeeded — saying "failed to load" here would
            // report the wrong thing about it. The list is stale until the next
            // refresh, and the Refresh button is right there.
            toast.info('Saved. The list could not be refreshed — use Refresh to see the change.');
            return null;
        }
    };

    const adopt = (detail: ScriptDetail): void => {
        setItemCache(prev => ({ ...prev, [detail.script_id]: detail.items ?? [] }));
        setShowProblems(false);
        setForm(
            withBaseline({
                scriptId: detail.script_id,
                productKey: detail.product_key,
                name: detail.name ?? '',
                hubspotProductId: detail.hubspot_product_id ?? '',
                notes: detail.notes ?? '',
                items: (detail.items ?? []).map(toDraft),
            }),
        );
    };

    const onSave = async (): Promise<void> => {
        if (!form || busy) return;
        if (problems.length) {
            setShowProblems(true);
            toast.error(problems[0]);
            return;
        }
        setBusy(true);
        try {
            if (form.scriptId === null) {
                const created = await createScript({
                    product_key: foldKey(form.productKey),
                    name: form.name.trim(),
                    hubspot_product_id: form.hubspotProductId.trim() || null,
                    notes: form.notes.trim() || null,
                    items: toInput(form.items ?? []),
                });
                adopt(created);
                await refresh();
                toast.success(`Draft saved as ${created.product_key} v${created.version}. Nobody sees it until it is approved.`);
                return;
            }

            const body: ScriptUpdate = {
                name: form.name.trim(),
                hubspot_product_id: form.hubspotProductId.trim() || null,
                notes: form.notes.trim() || null,
            };
            // OMITTED, not empty: the router keeps the current checklist for an
            // absent `items`, and this screen may not hold them (decision 4).
            if (form.items) body.items = toInput(form.items);

            const saved = await updateScript(form.scriptId, body);
            adopt(saved);
            await refresh();
            if (saved.forked) {
                toast.success(
                    `Saved as ${saved.product_key} v${saved.version}, a new draft. The live version is untouched — approve v${saved.version} when you want AEs to see it.`,
                );
            } else {
                toast.success('Draft saved.');
            }
        } catch (e) {
            const err = scriptsError(e, 'That script could not be saved.');
            toast.error(err.message);
            if (isAuthorDenied(err)) await refresh();
        } finally {
            setBusy(false);
        }
    };

    const onApprove = async (): Promise<void> => {
        if (!selected || busy) return;
        const count = form?.items ? form.items.length : selected.item_count;
        const ok = await confirmDialog({
            title: `Approve ${selected.name} v${selected.version}?`,
            message:
                `Approving is what puts a script in front of AEs. From now on, every client call your team captures for “${selected.product_key}” ` +
                `shows these ${count} question${count === 1 ? '' : 's'}, and the wrap-up reports any of them the call did not cover. ` +
                `Your name and the time are recorded as the approval, and cannot be changed afterwards.`,
            confirmLabel: 'Approve — put it on live calls',
        });
        if (!ok) return;
        setBusy(true);
        try {
            const res = await approveScript(selected.script_id);
            adopt(res);
            await refresh();
            if (res.already_approved) {
                toast.info(`Already approved by ${res.approved_by ?? 'someone'} — nothing changed.`);
            } else {
                toast.success(`Approved. AEs now see this checklist on calls for “${res.product_key}”.`);
            }
        } catch (e) {
            toast.error(scriptsError(e, 'That script could not be approved.').message);
        } finally {
            setBusy(false);
        }
    };

    const onRetire = async (): Promise<void> => {
        if (!selected || busy) return;
        const live = selected.is_live;
        const ok = await confirmDialog({
            title: `Retire ${selected.name} v${selected.version}?`,
            message: live
                ? `This is the live checklist for “${selected.product_key}”. Retiring it means AEs see no script for this product until another version is approved. Calls that already ran under it keep their record, and it can be restored.`
                : 'This version stops being offered. Calls that already ran under it keep their record, and it can be restored.',
            confirmLabel: 'Retire it',
            destructive: true,
        });
        if (!ok) return;
        setBusy(true);
        try {
            const res = await deactivateScript(selected.script_id);
            adopt(res);
            await refresh();
            toast.info(res.already_inactive ? 'That version was already retired.' : 'Retired. No AE sees it now.');
        } catch (e) {
            toast.error(scriptsError(e, 'That script could not be retired.').message);
        } finally {
            setBusy(false);
        }
    };

    const onRestore = async (): Promise<void> => {
        if (!selected || busy) return;
        setBusy(true);
        try {
            const res = await restoreScript(selected.script_id);
            adopt(res);
            const after = await refresh();
            const now = after?.scripts.find(s => s.script_id === res.script_id);
            toast.success(
                now?.is_live
                    ? `Restored, and it is live again for “${res.product_key}”.`
                    : 'Restored. It is not live — a newer approved version is.',
            );
        } catch (e) {
            toast.error(scriptsError(e, 'That script could not be restored.').message);
        } finally {
            setBusy(false);
        }
    };

    // ─── item editing ────────────────────────────────────────────────────────

    const setItems = (fn: (items: ItemDraft[]) => ItemDraft[]): void =>
        setForm(prev => (prev && prev.items ? { ...prev, items: fn(prev.items) } : prev));

    const patchItem = (uid: string, patch: Partial<ItemDraft>): void =>
        setItems(list => list.map(i => (i.uid === uid ? { ...i, ...patch } : i)));

    const moveItem = (index: number, delta: number): void =>
        setItems(list => {
            const target = index + delta;
            if (target < 0 || target >= list.length) return list;
            const next = [...list];
            const [moved] = next.splice(index, 1);
            next.splice(target, 0, moved);
            return next;
        });

    /** Turn "we do not hold this version's questions" into an empty, editable
     *  list — an explicit act, because saving afterwards REPLACES what is stored. */
    const onRewriteQuestions = async (): Promise<void> => {
        if (!form || !selected) return;
        const ok = await confirmDialog({
            title: 'Rewrite this checklist from scratch?',
            message:
                `Cubie cannot read this version's ${selected.item_count} saved question${selected.item_count === 1 ? '' : 's'} back into the editor. ` +
                'You can write a new list here, but saving will REPLACE the stored one. Until you do, the saved questions are untouched.',
            confirmLabel: 'Start a new list',
            destructive: true,
        });
        if (!ok) return;
        setForm(prev => (prev ? { ...prev, items: [blankItem()] } : prev));
    };

    // ─── the gate: may this person use this screen at all? ───────────────────
    //
    // After the hooks, never before — an early return above them would make the
    // hook order conditional. A `false` here is "not this person", never "the
    // feature is off"; `CCS_ENABLED` is a server setting the client never
    // receives and produces the `isNotEnabled` empty state further down.
    if (!canAccessCcs) {
        return (
            <PageContainer>
                <EosPageHeader
                    icon={<ListChecks size={20} />}
                    title="Call scripts"
                    right={<BackButton fallback="/" label="Home" />}
                />
                <EmptyState
                    title="Call scripts are not enabled for your account."
                    hint="Call scripts are part of Sales Call Capture, which an admin opens to teams in Admin → Feature Access. Nothing is wrong with your account — if you own a product your company sells and should be writing its checklist, ask a Cubie administrator."
                />
            </PageContainer>
        );
    }

    const header = (
        <EosPageHeader
            icon={<ListChecks size={20} />}
            title="Call scripts"
            subtitle="The facts each product's call has to surface — so an incomplete call stops looking like a complete one"
            right={
                <>
                    <button type="button" onClick={() => setReloadKey(k => k + 1)} className={BTN_GHOST} disabled={loading}>
                        <RefreshCw size={14} className={loading ? 'animate-spin' : undefined} />
                        Refresh
                    </button>
                    <BackButton fallback="/calls/start" label="Today's calls" />
                </>
            }
        />
    );

    if (loading) {
        return (
            <PageContainer>
                {header}
                <LoadingState label="Loading call scripts…" />
            </PageContainer>
        );
    }

    if (loadError) {
        return (
            <PageContainer>
                {header}
                {isNotEnabled(loadError) ? (
                    <EmptyState
                        title="Call scripts are not switched on in this environment."
                        hint="Sales Call Capture is off here, so there is nothing to author against yet. Nothing about your account is wrong."
                    />
                ) : isForbidden(loadError) ? (
                    <EmptyState title="Call scripts are not open to your account." hint={loadError.detail ?? undefined} />
                ) : (
                    <ErrorState message={loadError.message} />
                )}
            </PageContainer>
        );
    }

    return (
        <PageContainer>
            {header}

            {!canAuthor && (
                <div className={`${NOTICE_GRAY} mb-6`}>
                    <p className="text-sm text-gray-700">
                        You can read the live checklists here. Writing and approving them belongs to the people who own the products —
                        an admin adds them in Admin → Feature Access.
                    </p>
                </div>
            )}

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
                {/* ── the catalogue ─────────────────────────────────────────── */}
                <aside className="lg:col-span-1" data-tour="call-scripts-catalogue">
                    <div className="mb-3 flex items-center justify-between">
                        <h2 className="text-sm font-semibold text-gray-900">
                            {groups.length} product{groups.length === 1 ? '' : 's'}
                        </h2>
                        {canAuthor && (
                            <button type="button" data-tour="call-scripts-new" onClick={() => openNew()} className={BTN_GHOST} disabled={busy}>
                                <FilePlus2 size={14} />
                                New script
                            </button>
                        )}
                    </div>

                    {groups.length === 0 ? (
                        <div className={CARD}>
                            <p className="text-sm font-medium text-gray-700">No scripts yet.</p>
                            <p className="mt-2 text-sm text-gray-500">
                                {canAuthor
                                    ? 'Nothing is seeded — every product and every question here is yours to write. Start with one product you write SOWs for most often.'
                                    : 'Nobody has approved a checklist for any product yet.'}
                            </p>
                        </div>
                    ) : (
                        <ul className="space-y-4">
                            {groups.map(group => (
                                <li key={group.product_key}>
                                    <div className="mb-1.5 flex items-baseline justify-between gap-2">
                                        <span className="truncate text-sm font-semibold text-gray-900">{group.name}</span>
                                        {canAuthor && (
                                            <button
                                                type="button"
                                                onClick={() => openNew(group.product_key, group.name)}
                                                className="text-xs font-medium text-brand hover:underline"
                                            >
                                                New version
                                            </button>
                                        )}
                                    </div>
                                    <p className="mb-1.5 font-mono text-[11px] text-gray-400">{group.product_key}</p>
                                    {!group.live && (
                                        <p className="mb-1.5 text-xs text-amber-700">
                                            No approved version — AEs see no checklist for this product.
                                        </p>
                                    )}
                                    <ul className="space-y-1">
                                        {group.versions.map(version => {
                                            const state = scriptState(version);
                                            const active = form?.scriptId === version.script_id;
                                            return (
                                                <li key={version.script_id}>
                                                    <button
                                                        type="button"
                                                        onClick={() => void openScript(version)}
                                                        disabled={opening !== null}
                                                        className={
                                                            active
                                                                ? 'w-full rounded-md border border-brand bg-brand-tint px-3 py-2 text-left transition-colors'
                                                                : 'w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-left transition-colors hover:bg-gray-50'
                                                        }
                                                    >
                                                        <span className="flex items-center justify-between gap-2">
                                                            <span className="text-sm font-medium text-gray-800">v{version.version}</span>
                                                            <span className={STATE_BADGE[state]}>{SCRIPT_STATE_LABELS[state]}</span>
                                                        </span>
                                                        <span className="mt-0.5 block text-xs text-gray-500">
                                                            {version.item_count} question{version.item_count === 1 ? '' : 's'}
                                                            {version.required_item_count > 0 && `, ${version.required_item_count} required`}
                                                            {opening === version.script_id && ' · opening…'}
                                                        </span>
                                                    </button>
                                                </li>
                                            );
                                        })}
                                    </ul>
                                </li>
                            ))}
                        </ul>
                    )}
                </aside>

                {/* ── the editor ────────────────────────────────────────────── */}
                <section className="lg:col-span-2" data-tour="call-scripts-editor">
                    {!form ? (
                        <div className={CARD}>
                            <div className="flex items-start gap-3">
                                <ClipboardCheck className="mt-0.5 flex-shrink-0 text-brand" size={18} />
                                <div>
                                    <p className="text-base font-medium text-gray-800">Pick a version on the left, or write a new script.</p>
                                    <p className="mt-2 text-sm text-gray-600">{WHAT_IS_A_SCRIPT}</p>
                                    <p className="mt-3 text-sm text-gray-600">
                                        A script only reaches an AE once somebody approves it. Until then it is a draft nobody sees.
                                    </p>
                                </div>
                            </div>
                        </div>
                    ) : (
                        <div className="space-y-4">
                            {/* Identity + state */}
                            <div className={CARD}>
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                    <div>
                                        <h2 className="text-lg font-semibold text-gray-900">
                                            {isNew ? 'New script' : `${form.name || form.productKey} v${selected?.version ?? ''}`}
                                        </h2>
                                        <p className="mt-0.5 font-mono text-xs text-gray-400">{foldKey(form.productKey) || 'product_key'}</p>
                                    </div>
                                    {selected && (
                                        <div className="text-right">
                                            <span className={STATE_BADGE[scriptState(selected)]}>
                                                {SCRIPT_STATE_LABELS[scriptState(selected)]}
                                            </span>
                                            <p className="mt-1 max-w-xs text-xs text-gray-500">{SCRIPT_STATE_HINTS[scriptState(selected)]}</p>
                                        </div>
                                    )}
                                </div>
                                {selected && (
                                    <p className="mt-3 text-xs text-gray-500">
                                        {scriptSourceLabel(selected.source)}
                                        {selected.approved_by
                                            ? ` · approved by ${selected.approved_by}${selected.approved_at ? ` on ${selected.approved_at.slice(0, 10)}` : ''}`
                                            : ' · never approved'}
                                    </p>
                                )}
                            </div>

                            {/* Decision 2: the fork, said before the click. */}
                            {selected && savingForks(selected) && (
                                <div className={NOTICE_AMBER}>
                                    <div className="flex items-start gap-3">
                                        <TriangleAlert className="mt-0.5 flex-shrink-0 text-amber-600" size={18} />
                                        <div className="text-sm text-amber-900">
                                            <p className="font-medium">This version is approved, so editing it makes a new one.</p>
                                            <p className="mt-1">
                                                Saving writes <span className="font-medium">v{nextVersion}</span> as a draft and leaves v
                                                {selected.version} exactly as it is. Calls already running on v{selected.version} keep using
                                                it, and nothing you change here reaches an AE until you approve v{nextVersion}.
                                            </p>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* Fields */}
                            <div className={CARD}>
                                <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                                    <div>
                                        <label className={LABEL} htmlFor="script-product-key">
                                            Product key
                                        </label>
                                        <input
                                            id="script-product-key"
                                            className={showProblems && isNew && keyProblem(form.productKey, 'Product key') ? INPUT_BAD : INPUT}
                                            value={form.productKey}
                                            disabled={!isNew || !canAuthor}
                                            placeholder="operations_review"
                                            maxLength={MAX_KEY_CHARS}
                                            onChange={e => setForm(prev => (prev ? { ...prev, productKey: e.target.value } : prev))}
                                        />
                                        <p className="mt-1 text-xs text-gray-500">
                                            {isNew
                                                ? 'Lowercase, no spaces. It is the join key the whole feature matches on, so it never changes afterwards.'
                                                : 'A script is for one product. A different product is a different script.'}
                                        </p>
                                    </div>
                                    <div>
                                        <label className={LABEL} htmlFor="script-name">
                                            Name the AE sees
                                        </label>
                                        <input
                                            id="script-name"
                                            className={showProblems && !form.name.trim() ? INPUT_BAD : INPUT}
                                            value={form.name}
                                            disabled={!canAuthor}
                                            placeholder="Quarterly Operations Review"
                                            maxLength={MAX_NAME_CHARS}
                                            onChange={e => setForm(prev => (prev ? { ...prev, name: e.target.value } : prev))}
                                        />
                                    </div>
                                    <div>
                                        <label className={LABEL} htmlFor="script-hubspot-product">
                                            HubSpot product id <span className="font-normal normal-case text-gray-400">(optional)</span>
                                        </label>
                                        <input
                                            id="script-hubspot-product"
                                            className={INPUT}
                                            value={form.hubspotProductId}
                                            disabled={!canAuthor}
                                            maxLength={64}
                                            onChange={e => setForm(prev => (prev ? { ...prev, hubspotProductId: e.target.value } : prev))}
                                        />
                                    </div>
                                    <div>
                                        <label className={LABEL} htmlFor="script-notes">
                                            Notes for whoever approves this <span className="font-normal normal-case text-gray-400">(optional)</span>
                                        </label>
                                        <input
                                            id="script-notes"
                                            className={INPUT}
                                            value={form.notes}
                                            disabled={!canAuthor}
                                            maxLength={MAX_NOTES_CHARS}
                                            onChange={e => setForm(prev => (prev ? { ...prev, notes: e.target.value } : prev))}
                                        />
                                    </div>
                                </div>
                            </div>

                            {/* Questions */}
                            <div className={CARD}>
                                <div className="mb-3 flex items-center justify-between gap-2">
                                    <h3 className="text-sm font-semibold text-gray-900">
                                        Questions this call has to answer
                                        {items ? ` (${items.length})` : ''}
                                    </h3>
                                    {canAuthor && items && (
                                        <button
                                            type="button"
                                            onClick={() => setItems(list => [...list, blankItem()])}
                                            className={BTN_GHOST}
                                            disabled={items.length >= MAX_SCRIPT_ITEMS}
                                        >
                                            <Plus size={14} />
                                            Add a question
                                        </button>
                                    )}
                                </div>

                                {items === null ? (
                                    // Decision 4, said plainly rather than shown as an empty list.
                                    <div className={NOTICE_BLUE}>
                                        <div className="flex items-start gap-3">
                                            <Info className="mt-0.5 flex-shrink-0 text-blue-600" size={18} />
                                            <div className="text-sm text-blue-900">
                                                <p className="font-medium">
                                                    This version's {selected?.item_count ?? 0} saved question
                                                    {(selected?.item_count ?? 0) === 1 ? '' : 's'} cannot be shown here.
                                                </p>
                                                <p className="mt-1">
                                                    Cubie can only read a checklist back for the version that is currently live. The questions
                                                    are safe — saving from this screen changes only the name, product id and notes, and leaves
                                                    them exactly as they are.
                                                </p>
                                                {canAuthor && (
                                                    <button type="button" onClick={() => void onRewriteQuestions()} className={`${BTN_GHOST} mt-3`}>
                                                        Rewrite the checklist from scratch
                                                    </button>
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                ) : items.length === 0 ? (
                                    <div className={NOTICE_AMBER}>
                                        <p className="text-sm text-amber-900">
                                            No questions. A script with none reports every call as complete, because there is nothing to miss —
                                            so it cannot be approved until you add at least one.
                                        </p>
                                    </div>
                                ) : (
                                    <ul className="space-y-3">
                                        {items.map((item, index) => {
                                            const factBad = showProblems ? keyProblem(item.fact_key, 'Fact key') : null;
                                            const promptBad = showProblems && !item.prompt_text.trim();
                                            return (
                                                <li key={item.uid} className="rounded-md border border-gray-200 p-3">
                                                    <div className="mb-2 flex items-start justify-between gap-2">
                                                        <span className="text-xs font-semibold text-gray-400">#{index + 1}</span>
                                                        {canAuthor && (
                                                            <div className="flex items-center gap-1">
                                                                <button
                                                                    type="button"
                                                                    className={BTN_ICON}
                                                                    aria-label={`Move question ${index + 1} up`}
                                                                    disabled={index === 0}
                                                                    onClick={() => moveItem(index, -1)}
                                                                >
                                                                    <ArrowUp size={14} />
                                                                </button>
                                                                <button
                                                                    type="button"
                                                                    className={BTN_ICON}
                                                                    aria-label={`Move question ${index + 1} down`}
                                                                    disabled={index === items.length - 1}
                                                                    onClick={() => moveItem(index, 1)}
                                                                >
                                                                    <ArrowDown size={14} />
                                                                </button>
                                                                <button
                                                                    type="button"
                                                                    className={BTN_ICON}
                                                                    aria-label={`Remove question ${index + 1}`}
                                                                    onClick={() => setItems(list => list.filter(i => i.uid !== item.uid))}
                                                                >
                                                                    <Trash2 size={14} />
                                                                </button>
                                                            </div>
                                                        )}
                                                    </div>

                                                    <label className={LABEL} htmlFor={`prompt-${item.uid}`}>
                                                        What the AE asks
                                                    </label>
                                                    <textarea
                                                        id={`prompt-${item.uid}`}
                                                        className={promptBad ? INPUT_BAD : INPUT}
                                                        rows={2}
                                                        value={item.prompt_text}
                                                        disabled={!canAuthor}
                                                        maxLength={MAX_PROMPT_CHARS}
                                                        placeholder="How many hours per unit?"
                                                        onChange={e => patchItem(item.uid, { prompt_text: e.target.value })}
                                                        onBlur={() => {
                                                            // A fact key is the hardest part of this form for the people it is
                                                            // for, so suggest one — only into an EMPTY field, and always
                                                            // editable, because coverage matches it exactly and never fuzzily.
                                                            if (!canAuthor || item.fact_key.trim()) return;
                                                            const guess = suggestKey(item.prompt_text);
                                                            if (guess) patchItem(item.uid, { fact_key: guess });
                                                        }}
                                                    />

                                                    <div className="mt-2 grid grid-cols-1 gap-3 md:grid-cols-2">
                                                        <div>
                                                            <label className={LABEL} htmlFor={`fact-${item.uid}`}>
                                                                Fact key
                                                            </label>
                                                            <input
                                                                id={`fact-${item.uid}`}
                                                                className={factBad ? INPUT_BAD : INPUT}
                                                                value={item.fact_key}
                                                                disabled={!canAuthor}
                                                                maxLength={MAX_KEY_CHARS}
                                                                placeholder="hours_per_unit"
                                                                onChange={e => patchItem(item.uid, { fact_key: e.target.value })}
                                                            />
                                                            {factBad && <p className="mt-1 text-xs text-red-700">{factBad}</p>}
                                                        </div>
                                                        <div>
                                                            <label className={LABEL} htmlFor={`target-${item.uid}`}>
                                                                HubSpot field <span className="font-normal normal-case text-gray-400">(optional)</span>
                                                            </label>
                                                            <input
                                                                id={`target-${item.uid}`}
                                                                className={showProblems && targetProblem(item.hubspot_target) ? INPUT_BAD : INPUT}
                                                                value={item.hubspot_target}
                                                                disabled={!canAuthor}
                                                                maxLength={MAX_TARGET_CHARS}
                                                                placeholder="company.industry"
                                                                onChange={e => patchItem(item.uid, { hubspot_target: e.target.value })}
                                                            />
                                                        </div>
                                                    </div>

                                                    <div className="mt-2">
                                                        <label className={LABEL} htmlFor={`help-${item.uid}`}>
                                                            Note to the AE <span className="font-normal normal-case text-gray-400">(optional)</span>
                                                        </label>
                                                        <input
                                                            id={`help-${item.uid}`}
                                                            className={INPUT}
                                                            value={item.help_text}
                                                            disabled={!canAuthor}
                                                            maxLength={MAX_HELP_CHARS}
                                                            onChange={e => patchItem(item.uid, { help_text: e.target.value })}
                                                        />
                                                    </div>

                                                    <label className="mt-3 flex items-center gap-2 text-sm text-gray-700">
                                                        <input
                                                            type="checkbox"
                                                            className="h-4 w-4 rounded border-gray-300 text-brand focus:ring-brand disabled:opacity-40"
                                                            checked={item.required}
                                                            disabled={!canAuthor}
                                                            onChange={e => patchItem(item.uid, { required: e.target.checked })}
                                                        />
                                                        Required — the wrap-up flags the call when this is not covered
                                                    </label>
                                                </li>
                                            );
                                        })}
                                    </ul>
                                )}

                                <p className="mt-3 text-xs text-gray-500">
                                    Facts with no HubSpot field are still worth listing: hours per unit and a rate feed the SOW, not the CRM.
                                    The wrap-up surfaces them either way.
                                </p>
                            </div>

                            {/* Problems, only after an attempted save (decision 5) */}
                            {showProblems && problems.length > 0 && (
                                <div className="rounded-lg border border-red-200 bg-red-50 p-4">
                                    <p className="text-sm font-medium text-red-900">This script cannot be saved yet.</p>
                                    <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-red-800">
                                        {problems.map(p => (
                                            <li key={p}>{p}</li>
                                        ))}
                                    </ul>
                                </div>
                            )}

                            {/* Actions */}
                            {canAuthor && (
                                <div className="flex flex-wrap items-center gap-3">
                                    <button type="button" onClick={() => void onSave()} className={BTN_PRIMARY} disabled={busy || (!isNew && !dirty)}>
                                        <Save size={16} />
                                        {isNew
                                            ? 'Save as a draft'
                                            : selected && savingForks(selected)
                                              ? `Save as v${nextVersion} — a new draft`
                                              : 'Save draft'}
                                    </button>

                                    {selected && !selected.approved_by && selected.is_active && (
                                        <button type="button" onClick={() => void onApprove()} className={BTN_APPROVE} disabled={busy || dirty || selected.item_count === 0}>
                                            <CheckCircle2 size={16} />
                                            Approve — put this in front of AEs on live calls
                                        </button>
                                    )}

                                    {selected && selected.is_active && (
                                        <button type="button" onClick={() => void onRetire()} className={BTN_GHOST} disabled={busy}>
                                            <Trash2 size={14} />
                                            Retire this version
                                        </button>
                                    )}
                                    {selected && !selected.is_active && (
                                        <button type="button" onClick={() => void onRestore()} className={BTN_GHOST} disabled={busy}>
                                            <RotateCcw size={14} />
                                            Restore this version
                                        </button>
                                    )}

                                    {selected && !selected.approved_by && dirty && (
                                        <span className="text-xs text-gray-500">Save before approving — approval signs what is stored, not what is on screen.</span>
                                    )}
                                    {selected && !selected.approved_by && !dirty && selected.item_count === 0 && (
                                        <span className="text-xs text-gray-500">Add at least one question before approving.</span>
                                    )}
                                </div>
                            )}
                        </div>
                    )}
                </section>
            </div>
        </PageContainer>
    );
};

export default ScriptEditor;
