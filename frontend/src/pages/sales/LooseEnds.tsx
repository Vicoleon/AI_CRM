// Sales → Loose Ends (`/sales/loose-ends`): the AE's own list from the morning
// loop-closure scan, and — for Sales leaders — the owner-attributed Team view
// that is the L10 artifact. Design contract: docs/loose_ends/README.md.
//
// THE LIST IS THE SOURCE OF TRUTH, AND IT IS THE SERVER'S. Every write here
// (log it, draft a reply, not a real miss, remove a mute) is followed by a
// refetch, success or failure. Nothing is removed optimistically: an item
// leaves the list because the server says it closed, which is the whole
// premise of the feature ("closure is detected, never self-reported").
//
// FIVE RULES THIS PAGE KEEPS (README §2):
//
// 1. One real action per row. `log_in_hubspot` opens a preview of the AE's
//    OWN SENT EMAILS, read fresh from their mailbox, and the dialog's confirm
//    is the approval and the only thing that writes HubSpot: each email is
//    logged as an email, full text, never a note and never AI text (call notes
//    come from Fathom). `draft_reply` makes a Gmail DRAFT and never sends.
//    `open_link` opens Gmail / Slack / HubSpot in a new tab.
// 2. There is no "done" button. The only manual exit is "Not a real miss",
//    with a reason, and it is counted as noise on the Team tab.
// 3. "Could not look" is never "all clear". The coverage line prints what was
//    read, and every source that was not read gets a sentence above the list.
// 4. Every number carries its denominator ("2 of 6", "checked 52 email
//    threads"), on both tabs.
// 5. The Team tab carries names, kinds and ages only — never a subject, a
//    snippet or an AI headline — lists people in the server's A–Z order, and
//    paints everyone in the same brand colour. No leaderboard.
import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ChevronDown, ChevronRight, ExternalLink, Link2Off, RefreshCw, RotateCcw } from 'lucide-react';
import {
    AGING_BUCKETS,
    AGING_BUCKET_LABELS,
    DISMISS_REASON_LABELS,
    LOOSE_END_KINDS,
    LOOSE_END_KIND_LABELS,
    LOOSE_END_KIND_SHORT,
    MUTE_SCOPE_LABELS,
    closeReasonLabel,
    closedShareLine,
    coverageGaps,
    coverageLine,
    dismissLooseEnd,
    dismissReasonLabel,
    dismissReasonsFor,
    defaultLogSelection,
    draftLooseEndReply,
    emailsToLog,
    fetchLooseEndLogPreview,
    fetchLooseEnds,
    fetchLooseEndsTeam,
    isFixedCloseReason,
    isForbidden,
    isLooseEndActionKind,
    isLooseEndKind,
    isNotEnabled,
    logBatches,
    logButtonLabel,
    logLooseEnd,
    logTargetLine,
    looseEndKindLabel,
    looseEndsError,
    looseEndsWriteError,
    medianDaysLabel,
    mutePatternLabel,
    noiseLine,
    removeLooseEndsMute,
    scannedAtPhrase,
    setupLooseEndsScorecard,
} from '../../api/looseEnds';
import type {
    AgingBucket,
    DismissReason,
    LooseEnd,
    LooseEndAction,
    LooseEndAge,
    LooseEndsError,
    LooseEndsGroup,
    LooseEndsHealth,
    LooseEndsMine,
    LooseEndsMute,
    LooseEndsScorecardSetupResult,
    LooseEndsTeam,
    LogPreview,
    LogPreviewEmail,
    MuteScope,
    TeamPerson,
} from '../../api/looseEnds';
import {
    EmptyState,
    EosPageHeader,
    ErrorState,
    KpiCard,
    KpiStrip,
    LoadingState,
    OwnerChip,
    PageContainer,
} from '../eos/_shared';
import { Field, Modal, SubmitButton, inputCls } from '../../components/eos/Modal';
import { toast } from '../../components/eos/Toast';
import { PrimaryButton, SecondaryButton } from '../../components/smartTeams/shared';
import { AgeChip } from '../../components/sales';
import type { AgeChipProps } from '../../components/sales';
import { Tooltip } from '../../components/Tooltip';
import { useAuth } from '../../context/AuthContext';
import { parseInstant } from '../../utils/datetime';
import { scorecardControls } from './scorecardAccess';

const LIST_CARD = 'bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100';
const NOTE_CARD = 'rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900';
const CARD = 'rounded-lg border border-gray-200 bg-white p-4 shadow-sm';
const LINK_BUTTON =
    'inline-flex items-center gap-1 text-sm font-medium text-brand hover:underline focus:outline-none focus:ring-2 focus:ring-brand/30 rounded';

// Full literal class strings — the Tailwind JIT purges what it cannot see whole.
const TAB: Record<'on' | 'off', string> = {
    on: 'bg-brand text-white',
    off: 'text-gray-600 hover:bg-gray-50',
};

// One brand ramp for every person: the bar says "how old", never "who is
// worst" (README §2, no leaderboard). Full literal classes, keyed by bucket.
const AGING_BAR_CLASSES: Record<AgingBucket, string> = {
    d0_2: 'bg-brand-tint',
    d3_7: 'bg-brand/40',
    d8_14: 'bg-brand/70',
    d15_plus: 'bg-brand',
};

const HIDDEN_TITLE = 'Hidden while viewing as someone else';
const IMPERSONATION_NO_CHANGES = 'Cubie does not change someone else’s list while you are viewing as them.';

/** Opens a link out of Cubie. Every `href` here is Gmail, Slack or HubSpot. */
const openOut = (href: string): void => {
    window.open(href, '_blank', 'noopener,noreferrer');
};

/** The cockpit age chip, fed from this feature's `{state, label}` (`at_risk`
 *  below the escalation days, `breached` at or over them). The label is
 *  server-rendered ("4 days") and printed verbatim — never recomputed here. */
const ageProps = (age: LooseEndAge): AgeChipProps => ({ state: age.state, label: age.label });

const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** README order (unlogged, open_loop, todo); a kind a newer backend adds goes
 *  last rather than disappearing. */
const groupRank = (kind: string): number =>
    isLooseEndKind(kind) ? LOOSE_END_KINDS.indexOf(kind) : LOOSE_END_KINDS.length;

const formatDay = (value: string | null | undefined): string => {
    const at = parseInstant(value);
    return at ? at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';
};

// ─── page ────────────────────────────────────────────────────────────────────

type View = 'mine' | 'team';

export const LooseEnds: React.FC = () => {
    const { hasFeature } = useAuth();
    const [params, setParams] = useSearchParams();
    const [refreshKey, setRefreshKey] = useState(0);

    // The tab lives in the URL (?view=team), never mirrored into state, so a
    // leader can bookmark or paste the Team view. Without the team verdict the
    // param is ignored rather than showing a tab that would 403. A leader who
    // reads the roll-up but is not scanned themselves (a sales director: team
    // verdict only) has no Mine tab at all — it would 403 and invite them to
    // join the roster that starts a scan of their own mailbox.
    const canSeeTeam = hasFeature('loop_closure_team');
    const canSeeMine = hasFeature('loop_closure');
    const view: View = canSeeTeam && (params.get('view') === 'team' || !canSeeMine) ? 'team' : 'mine';
    const setView = (next: View): void => {
        setParams(
            prev => {
                const p = new URLSearchParams(prev);
                if (next === 'team') p.set('view', 'team');
                else p.delete('view');
                return p;
            },
            { replace: true },
        );
    };

    return (
        <PageContainer>
            <EosPageHeader
                icon={<Link2Off size={20} />}
                title="Loose Ends"
                subtitle={
                    view === 'team'
                        ? 'Who has what open, and for how long. Names and ages only — never the emails themselves.'
                        : 'What slipped through: not logged in HubSpot, waiting on your reply, or promised and not done.'
                }
                right={
                    <>
                        {canSeeTeam && canSeeMine && (
                            <div
                                role="tablist"
                                aria-label="Whose loose ends"
                                className="inline-flex rounded-lg border border-gray-200 bg-white p-0.5"
                                data-tour="loose-ends-tabs"
                            >
                                {(['mine', 'team'] as const).map(v => (
                                    <button
                                        key={v}
                                        type="button"
                                        role="tab"
                                        aria-selected={v === view}
                                        onClick={() => setView(v)}
                                        className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${v === view ? TAB.on : TAB.off}`}
                                    >
                                        {v === 'mine' ? 'Mine' : 'Team'}
                                    </button>
                                ))}
                            </div>
                        )}
                        <SecondaryButton onClick={() => setRefreshKey(k => k + 1)}>
                            <RefreshCw size={15} aria-hidden="true" />
                            Refresh
                        </SecondaryButton>
                    </>
                }
            />
            {view === 'team' ? <TeamView refreshKey={refreshKey} /> : <MineView refreshKey={refreshKey} />}
        </PageContainer>
    );
};

// ─── Mine ────────────────────────────────────────────────────────────────────

const MineView: React.FC<{ refreshKey: number }> = ({ refreshKey }) => {
    const [data, setData] = useState<LooseEndsMine | null>(null);
    const [error, setError] = useState<LooseEndsError | null>(null);
    const [writeKey, setWriteKey] = useState(0);
    // "this morning at 7:04 AM" needs a clock; taking it when the data lands
    // keeps the render pure and the phrase right for the data it describes.
    const [now, setNow] = useState(() => new Date());
    // Gmail draft links from this session, by item id. The item stays open
    // after a draft (it closes when the reply is SENT), so the row keeps the
    // link to the draft it already made.
    const [drafts, setDrafts] = useState<Record<string, string>>({});

    const [target, setTarget] = useState<LooseEnd | null>(null);
    // The row whose "Log it in HubSpot" preview is open.
    const [logTarget, setLogTarget] = useState<LooseEnd | null>(null);
    const [reason, setReason] = useState<DismissReason | ''>('');
    const [mute, setMute] = useState<MuteScope | 'none'>('none');
    const [submitting, setSubmitting] = useState(false);

    // Every setState sits inside the async callback (`react-hooks/set-state-in-
    // effect` is an error in this repo); the spinner is derived below.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchLooseEnds();
                if (cancelled) return;
                setData(fresh);
                setError(null);
                setNow(new Date());
            } catch (err) {
                if (!cancelled) setError(looseEndsError(err));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [refreshKey, writeKey]);

    const reload = (): void => setWriteKey(k => k + 1);

    /** Every write reports through a toast and then re-reads the list, success
     *  or failure: a 404 means the item already closed itself, and the fresh
     *  list is what says so. */
    const runWrite = async (op: () => Promise<string>): Promise<void> => {
        try {
            toast.success(await op());
        } catch (err) {
            toast.error(looseEndsWriteError(err));
        } finally {
            reload();
        }
    };

    const hidden = data?.content_hidden === true;

    // THE THREE WIRE STRINGS ARE THE ROUTER'S (README §5), pinned by
    // looseEnds.test.ts against backend/routers/loose_ends.py and
    // rubik_core/loop_closure/views.py. A kind this build does not know
    // degrades to its link, or to a disabled button that says why.
    const actionFor = (item: LooseEnd, action: LooseEndAction): RowAction => {
        const label = action.label;
        if (!isLooseEndActionKind(action.kind)) {
            const href = action.href;
            return href
                ? { label, run: async () => openOut(href) }
                : { label, run: async () => {}, disabledReason: `Cubie does not know how to "${label}" yet.` };
        }
        switch (action.kind) {
            // The one HubSpot write on this page. The click only OPENS the
            // preview: the AE's own sent emails, read fresh from their mailbox,
            // are shown before anything is written, and the dialog's confirm
            // is the approval. What is logged is those emails verbatim, as
            // HubSpot emails — never a note, never AI text.
            case 'log_in_hubspot':
                if (hidden) {
                    return {
                        label,
                        run: async () => {},
                        disabledReason: 'Cubie does not write to HubSpot while you are viewing as someone else.',
                    };
                }
                return { label, run: async () => setLogTarget(item) };
            // A Gmail DRAFT in the same thread. Never sends; the item stays
            // open until the reply actually goes out and the next scan sees it.
            case 'draft_reply':
                if (hidden) {
                    return {
                        label,
                        run: async () => {},
                        disabledReason: 'Cubie does not draft in someone else’s mailbox while you are viewing as them.',
                    };
                }
                return {
                    label,
                    run: () =>
                        runWrite(async () => {
                            const res = await draftLooseEndReply(item.id);
                            const url = res.draft_url;
                            if (url) setDrafts(prev => ({ ...prev, [item.id]: url }));
                            return url
                                ? 'Draft saved in Gmail, in the same thread. Nothing was sent — open it from the row to review and send.'
                                : 'Draft saved in Gmail, in the same thread. Nothing was sent — find it in your Drafts.';
                        }),
                };
            // Gmail, Slack or the HubSpot task, in a new tab.
            case 'open_link': {
                const href = action.href;
                if (!href) {
                    return {
                        label,
                        run: async () => {},
                        disabledReason: 'The link for this one is missing. Open it from Gmail or HubSpot directly.',
                    };
                }
                return { label, run: async () => openOut(href), href };
            }
            default:
                return { label, run: async () => {}, disabledReason: `Cubie does not know how to "${label}" yet.` };
        }
    };

    const openDismiss = (item: LooseEnd): void => {
        // Viewing as someone else: the server refuses every write, so the form
        // never opens (the button is disabled with the reason, too).
        if (hidden) return;
        setReason('');
        setMute('none');
        setTarget(item);
    };

    const onSubmitDismiss = async (e: React.FormEvent): Promise<void> => {
        e.preventDefault();
        if (!target || !reason || submitting) return;
        setSubmitting(true);
        try {
            await dismissLooseEnd(target.id, reason, target.source === 'gmail' && mute !== 'none' ? mute : null);
            toast.success(
                'Marked not a real miss. It counts toward the noise rate, which is how the scan gets tuned.',
            );
            setTarget(null);
        } catch (err) {
            // Stays open on failure — a modal that just closes reads as saved.
            toast.error(looseEndsWriteError(err));
        } finally {
            setSubmitting(false);
            reload();
        }
    };

    const onRemoveMute = (m: LooseEndsMute): Promise<void> =>
        runWrite(async () => {
            await removeLooseEndsMute(m.id);
            return `Removed. Cubie will flag ${mutePatternLabel(m.pattern)} again from the next scan.`;
        });

    // ─── states ─────────────────────────────────────────────────────────────

    if (data === null && error === null) return <LoadingState label="Loading your loose ends…" />;
    if (error && isNotEnabled(error)) {
        return (
            <EmptyState
                title="Loose Ends is not switched on yet."
                hint="Nothing is wrong with your account — the morning scan ships behind a switch and it is still off."
            />
        );
    }
    if (error && isForbidden(error)) {
        return (
            <EmptyState
                title="Loose Ends is not open to your account."
                hint="Your admin has limited the morning scan to some teams or people. If you should be on it, ask an admin to add you in Admin → Feature Access."
            />
        );
    }
    if (!data) return <ErrorState message={error?.message ?? 'Loose Ends could not be reached.'} />;

    const mutes = data.mutes ?? [];
    const mutesSection =
        mutes.length > 0 ? <MutesList mutes={mutes} hidden={hidden} onRemove={onRemoveMute} /> : null;

    // No coverage and nothing listed means no scan has covered this person yet
    // — someone added to the roster after this morning's run sees this too,
    // rather than three empty groups that would read as an all-clear. (Items
    // from an earlier scan still render, with the server's note, if the last
    // run missed them.)
    const neverScanned = !data.scanned_at || (!data.coverage && data.groups.every(g => g.items.length === 0));
    if (neverScanned) {
        return (
            <>
                <EmptyState
                    title="Cubie has not scanned your mail yet."
                    hint="The first scan runs on the next weekday morning. Your list appears here after it — there is nothing for you to set up."
                />
                {mutesSection}
            </>
        );
    }

    // The server's own sentences first; the client's copy only when an older
    // backend sent coverage without notes, so a gap is never silent.
    const gaps = coverageGaps(data.coverage);
    const notes = (data.notes ?? []).filter(Boolean);
    const noteLines = notes.length > 0 ? notes : gaps.map(g => g.sentence);
    const line = coverageLine(data.coverage, data.scanned_at, now);
    const escalation = data.thresholds?.escalation_days ?? 7;
    const groups = [...data.groups].sort((a, b) => groupRank(a.kind) - groupRank(b.kind));

    return (
        <>
            {line && <p className="mb-4 text-sm text-gray-600" data-tour="loose-ends-coverage">{line}</p>}

            {hidden && (
                <p className={`mb-4 ${NOTE_CARD}`} role="status">
                    You are viewing someone else&apos;s Loose Ends. Subjects and AI summaries are hidden,
                    and Cubie will not write to their HubSpot or Gmail from here.
                </p>
            )}

            {noteLines.length > 0 && (
                <div className={`mb-6 ${NOTE_CARD}`} role="status">
                    {noteLines.length === 1 ? (
                        <p>{noteLines[0]}</p>
                    ) : (
                        <ul className="list-disc space-y-1 pl-5">
                            {noteLines.map(n => (
                                <li key={n}>{n}</li>
                            ))}
                        </ul>
                    )}
                </div>
            )}

            <KpiStrip cols={4}>
                <KpiCard label="Open" value={data.totals.open} hint="Everything on your list right now." />
                <KpiCard
                    label={`Past ${escalation} days`}
                    value={
                        <>
                            {data.totals.over_escalation}
                            <span className="text-sm font-medium text-gray-500"> of {data.totals.open}</span>
                        </>
                    }
                    hint={`Open ${escalation} days or more — the number on the Sales scorecard.`}
                    valueClass={data.totals.over_escalation > 0 ? 'text-brand' : undefined}
                />
                <KpiCard
                    label="Closed this week"
                    value={data.totals.closed_this_week}
                    hint="Closed because Cubie saw the reply, the HubSpot log or the finished task."
                />
                <KpiCard
                    label="Flagged this week"
                    value={data.totals.flagged_this_week}
                    hint="New loose ends the scan found this week."
                />
            </KpiStrip>

            {groups.map(group => (
                <GroupSection
                    key={group.kind}
                    group={group}
                    hidden={hidden}
                    incomplete={gaps.length > 0}
                    drafts={drafts}
                    actionFor={actionFor}
                    onDismiss={openDismiss}
                />
            ))}

            {mutesSection}

            <Modal
                isOpen={target !== null}
                onClose={() => setTarget(null)}
                title="Not a real miss"
                maxWidth="max-w-lg"
            >
                {target && (
                    <form onSubmit={onSubmitDismiss} className="space-y-4">
                        <p className="text-sm text-gray-600">
                            {hidden ? looseEndKindLabel(target.kind) : target.title || '(no subject)'}
                            {[target.counterparty, target.company].filter(Boolean).length > 0 && (
                                <span className="block text-xs text-gray-500">
                                    {[target.counterparty, target.company].filter(Boolean).join(' · ')}
                                </span>
                            )}
                        </p>
                        <Field label="Why is this not a real miss?">
                            <select
                                required
                                className={inputCls}
                                value={reason}
                                onChange={e => setReason(e.target.value as DismissReason | '')}
                            >
                                <option value="" disabled>
                                    Pick a reason
                                </option>
                                {dismissReasonsFor(target.kind).map(r => (
                                    <option key={r} value={r}>
                                        {DISMISS_REASON_LABELS[r]}
                                    </option>
                                ))}
                            </select>
                        </Field>
                        {target.source === 'gmail' && (
                            <fieldset>
                                <legend className="mb-1.5 block text-sm font-medium text-gray-700">
                                    Future emails
                                </legend>
                                <div className="space-y-1.5">
                                    {(['none', 'sender', 'domain'] as const).map(scope => (
                                        <label key={scope} className="flex items-start gap-2 text-sm text-gray-700">
                                            <input
                                                type="radio"
                                                name="loose-end-mute"
                                                className="mt-0.5 accent-brand"
                                                checked={mute === scope}
                                                onChange={() => setMute(scope)}
                                            />
                                            <span>
                                                {scope === 'none' ? 'Keep flagging them' : MUTE_SCOPE_LABELS[scope]}
                                                {scope === 'sender' && target.counterparty && (
                                                    <span className="text-gray-500"> ({target.counterparty})</span>
                                                )}
                                                {scope === 'domain' && target.company && (
                                                    <span className="text-gray-500"> ({target.company})</span>
                                                )}
                                            </span>
                                        </label>
                                    ))}
                                </div>
                            </fieldset>
                        )}
                        <p className="text-xs text-gray-500">
                            This takes it off your list for good and counts as noise — the number the team
                            watches to tune the scan. It is not a way to mark something done: real loose
                            ends close on their own when Cubie sees the reply, the HubSpot log or the
                            finished task.
                        </p>
                        <SubmitButton disabled={!reason || submitting} onCancel={() => setTarget(null)}>
                            {submitting ? 'Saving…' : 'Mark not a real miss'}
                        </SubmitButton>
                    </form>
                )}
            </Modal>

            <Modal
                isOpen={logTarget !== null}
                onClose={() => setLogTarget(null)}
                title="Log it in HubSpot"
                maxWidth="max-w-3xl"
            >
                {logTarget && (
                    <LogEmailsPanel
                        key={logTarget.id}
                        item={logTarget}
                        onClose={() => setLogTarget(null)}
                        onLogged={message => {
                            toast.success(message);
                            setLogTarget(null);
                            reload();
                        }}
                        onFailed={reload}
                    />
                )}
            </Modal>
        </>
    );
};

// ─── "Log it in HubSpot": the preview and the confirm ───────────────────────

const ALERT = 'rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800';

/** What the AE approves: each email they sent in the thread, as HubSpot will
 *  get it, with a box each. Keyed on the item by the parent, so another row
 *  starts fresh. Only the emails that went to the item's own contact start
 *  ticked (`defaultLogSelection`): a forward to a partner or a personal
 *  address is logged only if the AE ticks it. The confirm sends the ticked
 *  emails HubSpot does not already have (`emailsToLog`), in batches of 20
 *  (`logBatches`); the server re-reads their content from the mailbox. */
const LogEmailsPanel: React.FC<{
    item: LooseEnd;
    onClose: () => void;
    onLogged: (message: string) => void;
    onFailed: () => void;
}> = ({ item, onClose, onLogged, onFailed }) => {
    const [preview, setPreview] = useState<LogPreview | null>(null);
    const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
    const [loadError, setLoadError] = useState<string | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [submitError, setSubmitError] = useState<string | null>(null);

    // Every setState sits inside the async callback (`react-hooks/set-state-in-
    // effect` is an error in this repo); the spinner is derived below.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const data = await fetchLooseEndLogPreview(item.id);
                if (!cancelled) {
                    setPreview(data);
                    setSelected(new Set(defaultLogSelection(data)));
                }
            } catch (err) {
                if (!cancelled) setLoadError(looseEndsWriteError(err));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [item.id]);

    if (preview === null && loadError === null) {
        return <LoadingState label="Reading the email from your mailbox…" />;
    }
    if (preview === null) {
        return (
            <div className="space-y-4">
                <p role="alert" className={ALERT}>
                    {loadError}
                </p>
                <div className="flex justify-end">
                    <SecondaryButton onClick={onClose}>Close</SecondaryButton>
                </div>
            </div>
        );
    }

    const pending = preview.emails.filter(e => !e.already_logged);
    const toLog = emailsToLog(preview, selected);
    const where = logTargetLine(preview);
    const contactName = preview.contact?.trim() || 'the contact';

    const toggle = (messageId: string): void => {
        setSelected(prev => {
            const next = new Set(prev);
            if (next.has(messageId)) next.delete(messageId);
            else next.add(messageId);
            return next;
        });
    };

    const onConfirm = async (): Promise<void> => {
        if (submitting || toLog.length === 0) return;
        setSubmitting(true);
        setSubmitError(null);
        const skipped = pending.length - toLog.length;
        try {
            const batches = logBatches(toLog);
            let n = 0;
            for (let i = 0; i < batches.length; i += 1) {
                const res = await logLooseEnd(item.id, batches[i], i < batches.length - 1);
                n += res.logged ?? 0;
            }
            const done =
                n > 0
                    ? `Logged ${plural(n, 'email')} in HubSpot. It is off your list.`
                    : 'Already in HubSpot. It is off your list.';
            onLogged(
                skipped > 0 ? `${done} The ${plural(skipped, 'email')} you left unticked ${skipped === 1 ? 'was' : 'were'} not logged.` : done,
            );
        } catch (err) {
            // Stays open on failure, with the reason. Any email written before
            // the failure is remembered by the server, so the refreshed preview
            // shows it as already in HubSpot and trying again never logs it twice.
            setSubmitError(looseEndsWriteError(err));
            onFailed();
            try {
                setPreview(await fetchLooseEndLogPreview(item.id));
            } catch {
                // Keep the preview already shown; the error above says why.
            }
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div className="space-y-4">
            {preview.emails.length === 0 ? (
                <p className="text-sm text-gray-700">
                    Cubie could not find an email you sent to this client in that thread, so there is
                    nothing to log. Open it in Gmail or HubSpot instead.
                </p>
            ) : (
                <>
                    <p className="text-sm text-gray-700">
                        Cubie logs your actual {preview.emails.length === 1 ? 'email' : 'emails'}, full
                        text, on <span className="font-medium">{where}</span> in HubSpot, the same way
                        HubSpot&apos;s Gmail extension does. Nothing is rewritten or summarised, no note
                        is added, and nothing is written until you confirm. Only the emails you tick are
                        logged: the ones you sent to {contactName} start ticked, and anything else (a
                        forward to a partner, say) is logged only if you tick it.
                    </p>
                    <ul className="space-y-3">
                        {preview.emails.map(email => (
                            <LogEmailCard
                                key={email.message_id}
                                email={email}
                                where={where}
                                contactName={contactName}
                                checked={!email.already_logged && selected.has(email.message_id)}
                                disabled={submitting}
                                onToggle={() => toggle(email.message_id)}
                            />
                        ))}
                    </ul>
                </>
            )}

            {submitError && (
                <p role="alert" className={ALERT}>
                    {submitError}
                </p>
            )}

            <div className="flex items-center justify-end gap-3 pt-2">
                {pending.length > 0 ? (
                    <>
                        <SecondaryButton onClick={onClose} disabled={submitting}>
                            Cancel
                        </SecondaryButton>
                        <PrimaryButton
                            onClick={() => void onConfirm()}
                            busy={submitting}
                            disabled={toLog.length === 0}
                        >
                            {toLog.length > 0 ? logButtonLabel(toLog.length) : 'Tick an email to log'}
                        </PrimaryButton>
                    </>
                ) : preview.emails.length > 0 ? (
                    <PrimaryButton onClick={onClose}>Already in HubSpot. Close</PrimaryButton>
                ) : (
                    <SecondaryButton onClick={onClose}>Close</SecondaryButton>
                )}
            </div>
        </div>
    );
};

const LogEmailCard: React.FC<{
    email: LogPreviewEmail;
    where: string;
    contactName: string;
    checked: boolean;
    disabled: boolean;
    onToggle: () => void;
}> = ({ email, where, contactName, checked, disabled, onToggle }) => {
    const sent = parseInstant(email.sent_at);
    const cc = email.cc ?? [];
    const more = email.chars > email.excerpt.length;
    const boxId = `log-email-${email.message_id}`;
    return (
        <li
            className={`rounded-lg border p-3 ${
                email.already_logged ? 'border-gray-200 bg-gray-50 opacity-60' : 'border-gray-200 bg-white'
            }`}
        >
            <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex items-start gap-2">
                    {!email.already_logged && (
                        <input
                            id={boxId}
                            type="checkbox"
                            className="mt-1 accent-brand"
                            checked={checked}
                            disabled={disabled}
                            onChange={onToggle}
                        />
                    )}
                    <label
                        htmlFor={email.already_logged ? undefined : boxId}
                        className="text-sm font-semibold text-gray-900"
                    >
                        {email.subject || '(no subject)'}
                    </label>
                </div>
                {email.already_logged && (
                    <span className="rounded-full border border-gray-300 bg-white px-2 py-0.5 text-xs font-medium text-gray-600">
                        Already in HubSpot
                    </span>
                )}
            </div>
            <p className="mt-1 text-xs text-gray-600">To: {email.to.length > 0 ? email.to.join(', ') : '—'}</p>
            {cc.length > 0 && <p className="text-xs text-gray-600">Cc: {cc.join(', ')}</p>}
            {sent && (
                <p className="text-xs text-gray-500">
                    Sent{' '}
                    {sent.toLocaleString(undefined, {
                        weekday: 'short',
                        month: 'short',
                        day: 'numeric',
                        hour: 'numeric',
                        minute: '2-digit',
                    })}
                </p>
            )}
            {!email.already_logged && email.to_contact !== true && (
                <p className="mt-1 text-xs text-amber-700">
                    This one did not go to {contactName}, so it is not ticked. Tick it only if it belongs on
                    their HubSpot record.
                </p>
            )}
            <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-gray-100 bg-gray-50 p-2 font-sans text-xs text-gray-700">
                {email.excerpt}
                {more ? '…' : ''}
            </pre>
            <p className="mt-2 text-xs text-gray-500">
                {email.already_logged
                    ? 'HubSpot already has this email, so it will not be logged again.'
                    : checked
                      ? `Full text (${email.chars.toLocaleString()} characters) will be logged as an email on ${where}. Attachments are not included.`
                      : 'Not ticked, so it will not be logged.'}
                {!email.already_logged &&
                    email.truncated &&
                    ' It is very long, so the text is cut at 65,000 characters, the most HubSpot holds.'}
                {!email.already_logged &&
                    email.plain_text_only &&
                    ' Its formatted version is too long for HubSpot, so it is logged as plain text.'}
            </p>
        </li>
    );
};

interface RowAction {
    label: string;
    run: () => Promise<void>;
    /** Set when the action cannot run: the button renders disabled with this
     *  sentence as its tooltip. */
    disabledReason?: string;
    /** Set for a link out: rendered as a real anchor where it is secondary. */
    href?: string;
}

const GroupSection: React.FC<{
    group: LooseEndsGroup;
    hidden: boolean;
    incomplete: boolean;
    drafts: Record<string, string>;
    actionFor: (item: LooseEnd, action: LooseEndAction) => RowAction;
    onDismiss: (item: LooseEnd) => void;
}> = ({ group, hidden, incomplete, drafts, actionFor, onDismiss }) => {
    const headingId = `loose-ends-${group.kind}`;
    return (
        <section className="mb-8" aria-labelledby={headingId} data-tour={group.kind === LOOSE_END_KINDS[0] ? 'loose-ends-groups' : undefined}>
            <div className="mb-3">
                <h2 id={headingId} className="text-lg font-semibold text-gray-900">
                    {group.label}
                    <span className="ml-2 text-sm font-normal text-gray-500">{group.count}</span>
                </h2>
                <p className="mt-0.5 text-xs text-gray-500">{group.hint}</p>
            </div>

            {group.items.length === 0 ? (
                <div className="px-1 text-sm text-gray-500">
                    <p>{group.empty_line}</p>
                    {/* An empty list next to a source Cubie could not read is
                        "could not look", not "all clear" — say so here too. */}
                    {incomplete && (
                        <p className="mt-1 text-xs text-amber-800">
                            Not every source could be read on the last scan, so this may not be complete —
                            see the note above.
                        </p>
                    )}
                </div>
            ) : (
                <div className={LIST_CARD}>
                    {group.items.map(item => {
                        // One primary per row by contract. Should a row ever
                        // arrive without one, its link is promoted rather than
                        // leaving a row with nothing to do.
                        const primary = item.primary_action ?? item.secondary_action ?? null;
                        const secondary = item.primary_action ? item.secondary_action : null;
                        return (
                            <LooseEndRow
                                key={item.id}
                                item={item}
                                hidden={hidden}
                                draftUrl={drafts[item.id]}
                                primary={primary ? actionFor(item, primary) : undefined}
                                secondary={secondary ? actionFor(item, secondary) : undefined}
                                onDismiss={() => onDismiss(item)}
                            />
                        );
                    })}
                </div>
            )}

            {group.count > group.items.length && (
                <p className="mt-2 px-1 text-xs text-gray-500">
                    Showing the {group.items.length} oldest of {group.count}.
                </p>
            )}
        </section>
    );
};

const LooseEndRow: React.FC<{
    item: LooseEnd;
    hidden: boolean;
    draftUrl?: string;
    primary?: RowAction;
    secondary?: RowAction;
    onDismiss: () => void;
}> = ({ item, hidden, draftUrl, primary, secondary, onDismiss }) => {
    const [busy, setBusy] = useState(false);
    const who = [item.counterparty, item.company].filter(Boolean).join(' · ');

    // The usual outcome of a write is a refetch that removes this row; the
    // finally-clause then pokes an unmounted component, which React 19
    // tolerates silently. The write itself reports through a toast.
    const runPrimary = (): void => {
        if (busy || !primary || primary.disabledReason) return;
        setBusy(true);
        void primary.run().finally(() => setBusy(false));
    };

    let primaryNode: React.ReactNode = null;
    if (primary) {
        const button = (
            <PrimaryButton onClick={runPrimary} busy={busy} disabled={!!primary.disabledReason}>
                {primary.label}
            </PrimaryButton>
        );
        // A disabled button swallows hover, so the reason sits on a wrapper
        // (Tooltip is focusable, so it is reachable by keyboard too).
        primaryNode = primary.disabledReason ? <Tooltip content={primary.disabledReason}>{button}</Tooltip> : button;
    }

    let secondaryNode: React.ReactNode = null;
    if (secondary) {
        if (secondary.href && !secondary.disabledReason) {
            secondaryNode = (
                <a href={secondary.href} target="_blank" rel="noopener noreferrer" className={LINK_BUTTON}>
                    {secondary.label}
                    <ExternalLink size={13} aria-hidden="true" />
                </a>
            );
        } else if (!secondary.disabledReason) {
            secondaryNode = (
                <button type="button" className={LINK_BUTTON} onClick={() => void secondary.run()}>
                    {secondary.label}
                </button>
            );
        }
    }

    return (
        <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 flex-1">
                <p className="text-base font-semibold text-gray-900">
                    {hidden ? (
                        <span className="font-normal italic text-gray-500">{HIDDEN_TITLE}</span>
                    ) : (
                        item.title || '(no subject)'
                    )}
                </p>
                {!hidden && item.headline && <p className="mt-0.5 text-sm text-gray-600">{item.headline}</p>}
                {who && <p className="mt-1 text-xs text-gray-500">{who}</p>}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                    {item.age && <AgeChip {...ageProps(item.age)} />}
                    {item.reopened && (
                        <span
                            className="inline-flex items-center gap-1 rounded-full border border-gray-300 bg-white px-2 py-0.5 text-xs font-medium text-gray-600"
                            title={
                                item.kind === 'todo'
                                    ? 'It was rescheduled once, and it is now past its new date too.'
                                    : 'It closed once, and a new email in the same thread is not in HubSpot yet.'
                            }
                        >
                            <RotateCcw size={12} aria-hidden="true" />
                            Reopened
                        </span>
                    )}
                    {draftUrl && (
                        <button type="button" className={LINK_BUTTON} onClick={() => openOut(draftUrl)}>
                            Open the draft in Gmail
                            <ExternalLink size={13} aria-hidden="true" />
                        </button>
                    )}
                </div>
            </div>

            <div className="flex flex-shrink-0 flex-wrap items-center gap-3 sm:justify-end">
                {secondaryNode}
                {primaryNode}
                {hidden ? (
                    <Tooltip content={IMPERSONATION_NO_CHANGES}>
                        <button type="button" disabled className="cursor-not-allowed text-sm text-gray-400">
                            Not a real miss
                        </button>
                    </Tooltip>
                ) : (
                    <button
                        type="button"
                        onClick={onDismiss}
                        className="text-sm text-gray-500 underline-offset-2 hover:text-gray-800 hover:underline"
                    >
                        Not a real miss
                    </button>
                )}
            </div>
        </div>
    );
};

const MutesList: React.FC<{
    mutes: LooseEndsMute[];
    hidden: boolean;
    onRemove: (m: LooseEndsMute) => Promise<void>;
}> = ({ mutes, hidden, onRemove }) => {
    const [removing, setRemoving] = useState<string | null>(null);
    const remove = (m: LooseEndsMute): void => {
        if (removing || hidden) return;
        setRemoving(m.id);
        void onRemove(m).finally(() => setRemoving(null));
    };
    return (
        <section className="mt-10 border-t border-gray-200 pt-6" aria-labelledby="loose-ends-mutes">
            <h2 id="loose-ends-mutes" className="text-base font-semibold text-gray-900">
                Senders and domains you muted
            </h2>
            <p className="mt-0.5 text-xs text-gray-500">
                Cubie does not flag these for you. Remove one to start flagging it again from the next scan.
            </p>
            <ul className={`mt-3 ${LIST_CARD}`}>
                {mutes.map(m => (
                    <li key={m.id} className="flex items-center justify-between gap-3 px-5 py-3">
                        <div className="min-w-0">
                            <p className="truncate text-sm font-medium text-gray-900">{mutePatternLabel(m.pattern)}</p>
                            {m.created_at && <p className="text-xs text-gray-500">Muted {formatDay(m.created_at)}</p>}
                        </div>
                        {hidden ? (
                            <Tooltip content={IMPERSONATION_NO_CHANGES}>
                                <SecondaryButton onClick={() => {}} disabled>
                                    Remove
                                </SecondaryButton>
                            </Tooltip>
                        ) : (
                            <SecondaryButton
                                onClick={() => remove(m)}
                                busy={removing === m.id}
                                disabled={removing !== null}
                            >
                                Remove
                            </SecondaryButton>
                        )}
                    </li>
                ))}
            </ul>
        </section>
    );
};

// ─── Team ────────────────────────────────────────────────────────────────────

const TeamView: React.FC<{ refreshKey: number }> = ({ refreshKey }) => {
    const { canAccessEos } = useAuth();
    const [data, setData] = useState<LooseEndsTeam | null>(null);
    const [error, setError] = useState<LooseEndsError | null>(null);
    const [writeKey, setWriteKey] = useState(0);
    const [now, setNow] = useState(() => new Date());
    const [settingUp, setSettingUp] = useState(false);
    const [setupResult, setSetupResult] = useState<LooseEndsScorecardSetupResult | null>(null);
    const scorecardAccess = scorecardControls(canAccessEos, data?.scorecard);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const fresh = await fetchLooseEndsTeam();
                if (cancelled) return;
                setData(fresh);
                setError(null);
                setNow(new Date());
            } catch (err) {
                if (!cancelled) setError(looseEndsError(err));
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [refreshKey, writeKey]);

    const onSetup = async (): Promise<void> => {
        if (settingUp || !scorecardAccess.canSetup) return;
        setSettingUp(true);
        try {
            const res = await setupLooseEndsScorecard();
            setSetupResult(res);
            toast.success(
                res.created > 0
                    ? `Added ${plural(res.created, 'scorecard row')}. Cubie fills them in each week.`
                    : 'No rows were added — see the note under the button.',
            );
        } catch (err) {
            toast.error(looseEndsWriteError(err));
        } finally {
            setSettingUp(false);
            setWriteKey(k => k + 1);
        }
    };

    if (data === null && error === null) return <LoadingState label="Loading the team view…" />;
    if (error && isNotEnabled(error)) {
        return (
            <EmptyState
                title="Loose Ends is not switched on yet."
                hint="The morning scan ships behind a switch and it is still off."
            />
        );
    }
    if (error && isForbidden(error)) {
        return (
            <EmptyState
                title="The team view is not open to your account."
                hint="It is for Sales leaders. Your own list is on the Mine tab."
            />
        );
    }
    if (!data) return <ErrorState message={error?.message ?? 'The team view could not be reached.'} />;

    if (!data.scanned_at) {
        return (
            <EmptyState
                title="No scan has run yet."
                hint="The first scan runs on the next weekday morning; the team view fills in after it."
            />
        );
    }

    const { health } = data;
    const { scorecard, canSetup } = scorecardAccess;
    const escalation = data.thresholds?.escalation_days ?? 7;
    const openTotal = data.people.reduce((n, p) => n + (p.open ?? 0), 0);
    const overTotal = data.people.reduce((n, p) => n + (p.over_escalation ?? 0), 0);
    const when = scannedAtPhrase(data.scanned_at, now);
    const nameOf = (email: string): string => data.people.find(p => p.email === email)?.name || email;
    const noSeat = (scorecard?.people_without_seat ?? []).map(nameOf);

    return (
        <>
            <p className="mb-4 text-sm text-gray-600">
                {when ? `Last scan ran ${when}. ` : ''}
                {plural(data.roster_size, 'person', 'people')} on the Loose Ends roster.
            </p>

            {health ? (
                <HealthSection
                    health={health}
                    openTotal={openTotal}
                    overTotal={overTotal}
                    peopleCount={data.people.length}
                    escalation={escalation}
                />
            ) : (
                // Missing is said out loud rather than printed as zeros: a 0%
                // noise rate on no data would read as a perfect detector.
                <p className={`mb-6 ${NOTE_CARD}`} role="status">
                    The health numbers (noise rate, days to close) did not come back with this read, so
                    they are not shown rather than shown as zero. {openTotal} open across{' '}
                    {plural(data.people.length, 'person', 'people')}.
                </p>
            )}

            <section aria-labelledby="loose-ends-people" data-tour="loose-ends-team-people">
                <h2 id="loose-ends-people" className="text-lg font-semibold text-gray-900">
                    People
                    <span className="ml-2 text-sm font-normal text-gray-500">A–Z</span>
                </h2>
                <p className="mt-0.5 mb-3 text-xs text-gray-500">
                    Names, kinds and ages only. A badge means Cubie could not read that source for that
                    person on the last scan, so their count may be low rather than clean.
                </p>
                {data.people.length === 0 ? (
                    <p className="px-1 text-sm text-gray-500">Nobody on the roster has been scanned yet.</p>
                ) : (
                    <div className="space-y-3">
                        {/* Server order, A–Z by name. Never re-sorted by count:
                            this is not a leaderboard (README §2). */}
                        {data.people.map(person => (
                            <PersonCard key={person.email} person={person} escalation={escalation} />
                        ))}
                    </div>
                )}
            </section>

            {scorecard && (
                <section className={`mt-8 ${CARD}`} aria-labelledby="loose-ends-scorecard" data-tour="loose-ends-team-scorecard">
                    <h2 id="loose-ends-scorecard" className="text-base font-semibold text-gray-900">
                        Scorecard: &ldquo;{scorecard.metric_name}&rdquo;
                    </h2>
                    <p className="mt-1 text-sm text-gray-600">
                        {scorecard.people_without_metric > 0
                            ? `${scorecard.people_without_metric} of ${plural(data.roster_size, 'person', 'people')} on the roster have no “${scorecard.metric_name}” row on the Sales scorecard yet. Cubie fills each row in weekly from this list.`
                            : `Everyone on the roster (${plural(data.roster_size, 'person', 'people')}) has a “${scorecard.metric_name}” row on the Sales scorecard. Cubie fills it in weekly from this list.`}
                    </p>
                    {canSetup && (
                        <div className="mt-3">
                            <PrimaryButton onClick={() => void onSetup()} busy={settingUp}>
                                Add the scorecard rows
                            </PrimaryButton>
                        </div>
                    )}
                    {noSeat.length > 0 && (
                        // From the read itself, so it survives a refresh: these
                        // are the people "Add the scorecard rows" skips every time.
                        <p className="mt-2 text-xs text-gray-500">
                            {noSeat.length} of the {scorecard.people_without_metric} without a row{' '}
                            {noSeat.length === 1 ? 'has' : 'have'} no seat on the Accountability Chart, so
                            Cubie cannot add one until they get a seat: {noSeat.join(', ')}.
                        </p>
                    )}
                    {setupResult && (
                        <p className="mt-3 text-xs text-gray-600">
                            Added {setupResult.created}; {setupResult.already} already had one.
                            {setupResult.skipped_no_person.length > 0 &&
                                ` ${plural(setupResult.skipped_no_person.length, 'person', 'people')} could not get a row because they have no seat on the Accountability Chart: ${setupResult.skipped_no_person.join(', ')}.`}
                        </p>
                    )}
                </section>
            )}
        </>
    );
};

/** The spec's success metric, and how closes happened. Every figure names what
 *  it is out of (README §2). */
const HealthSection: React.FC<{
    health: LooseEndsHealth;
    openTotal: number;
    overTotal: number;
    peopleCount: number;
    escalation: number;
}> = ({ health, openTotal, overTotal, peopleCount, escalation }) => {
    const median = medianDaysLabel(health.median_days_to_close);
    const pct = health.flagged
        ? `${Math.round((health.noise_rate ?? health.dismissed / health.flagged) * 100)}%`
        : '—';
    const closedReasons = Object.entries(health.closed_by_reason ?? {});
    const dismissedReasons = Object.entries(health.dismissed_by_reason ?? {});
    const weekly = health.weekly ?? [];

    return (
        <>
            <KpiStrip cols={3}>
                <HealthTile
                    label="Open now"
                    value={openTotal}
                    line={`${overTotal} of ${openTotal} open ${escalation} days or more, across ${plural(peopleCount, 'person', 'people')}.`}
                />
                <HealthTile
                    label="Not a real miss"
                    value={pct}
                    line={
                        health.flagged ? `${noiseLine(health)}, last ${health.window_days} days.` : noiseLine(health)
                    }
                />
                <HealthTile
                    label="Median days to close"
                    value={median ?? '—'}
                    line={
                        // The cohort the server measures: items FLAGGED in the
                        // window, of which these have closed (README §5).
                        !health.flagged
                            ? `Nothing flagged in the last ${health.window_days} days.`
                            : health.closed > 0
                              ? `Across the ${health.closed} of ${health.flagged} loose ends flagged in the last ${health.window_days} days that have closed.`
                              : `None of the ${health.flagged} flagged in the last ${health.window_days} days has closed yet.`
                    }
                />
            </KpiStrip>

            <div className="mb-6 grid gap-3 md:grid-cols-2" data-tour="loose-ends-team-health">
                <div className={CARD}>
                    <h3 className="text-sm font-semibold text-gray-900">How they closed</h3>
                    <p className="mt-0.5 text-xs text-gray-500">
                        Loose ends flagged in the last {health.window_days} days that have closed. Every close
                        was detected — nobody can mark one done.
                    </p>
                    {closedReasons.length === 0 ? (
                        <p className="mt-3 text-sm text-gray-500">Nothing closed yet.</p>
                    ) : (
                        <ul className="mt-3 space-y-1.5 text-sm">
                            {closedReasons.map(([reason, count]) => (
                                <li key={reason} className="flex items-baseline justify-between gap-3">
                                    <span className="text-gray-700">{closeReasonLabel(reason)}</span>
                                    {/* A reason that is not a fix (rescheduled) is
                                        not part of the closed total, so it is never
                                        printed as a share of it. */}
                                    <span className="tabular-nums text-gray-500">
                                        {isFixedCloseReason(reason)
                                            ? closedShareLine(count, health.closed)
                                            : `${count}, not in the ${health.closed}`}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
                <div className={CARD}>
                    <h3 className="text-sm font-semibold text-gray-900">Why flags were not real misses</h3>
                    <p className="mt-0.5 text-xs text-gray-500">
                        The noise the scan is tuned on. Over 20% from newsletters or auto-replies means the
                        reply window should widen first.
                    </p>
                    {dismissedReasons.length === 0 ? (
                        <p className="mt-3 text-sm text-gray-500">No flag has been marked not a real miss.</p>
                    ) : (
                        <ul className="mt-3 space-y-1.5 text-sm">
                            {dismissedReasons.map(([reason, count]) => (
                                <li key={reason} className="flex items-baseline justify-between gap-3">
                                    <span className="text-gray-700">{dismissReasonLabel(reason)}</span>
                                    <span className="tabular-nums text-gray-500">
                                        {closedShareLine(count, health.dismissed)}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </div>

            {weekly.length > 0 && (
                <div className={`mb-8 overflow-x-auto ${CARD}`}>
                    <h3 className="text-sm font-semibold text-gray-900">Week by week</h3>
                    <table className="mt-3 w-full text-left text-sm">
                        <thead>
                            <tr className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                                <th scope="col" className="py-2 pr-4 font-semibold">Week of</th>
                                <th scope="col" className="py-2 pr-4 font-semibold">Flagged</th>
                                <th scope="col" className="py-2 pr-4 font-semibold">Closed</th>
                                <th scope="col" className="py-2 font-semibold">Median days to close</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {weekly.map(w => (
                                <tr key={w.week_start}>
                                    <td className="py-2 pr-4 text-gray-700">{formatDay(w.week_start)}</td>
                                    <td className="py-2 pr-4 tabular-nums text-gray-700">{w.flagged}</td>
                                    <td className="py-2 pr-4 tabular-nums text-gray-700">{w.closed}</td>
                                    <td className="py-2 tabular-nums text-gray-700">
                                        {medianDaysLabel(w.median_days_to_close) ?? '—'}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </>
    );
};

const HealthTile: React.FC<{ label: string; value: React.ReactNode; line: string }> = ({ label, value, line }) => (
    <div className="rounded-lg border border-gray-200 bg-white px-4 py-3.5 shadow-sm">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">{label}</div>
        <div className="mt-0.5 text-2xl font-bold leading-tight tracking-tight text-gray-900">{value}</div>
        <p className="mt-1 text-xs text-gray-500">{line}</p>
    </div>
);

const PersonCard: React.FC<{ person: TeamPerson; escalation: number }> = ({ person, escalation }) => {
    const [open, setOpen] = useState(false);
    const kinds = LOOSE_END_KINDS.map(k => [k, person.by_kind?.[k] ?? 0] as const).filter(([, n]) => n > 0);
    const gaps = coverageGaps(person.coverage);
    const listId = `loose-ends-items-${person.email}`;

    return (
        <div className={CARD}>
            <div className="flex flex-wrap items-start justify-between gap-3">
                <OwnerChip name={person.name || person.email} size="md" />
                <p className="text-sm text-gray-600">
                    {person.open} open
                    <span className="text-gray-400"> · </span>
                    {person.open > 0 && (
                        <>
                            {person.over_escalation} of {person.open} open {escalation}+ days
                            <span className="text-gray-400"> · </span>
                        </>
                    )}
                    {person.closed_this_week} closed this week
                </p>
            </div>

            {kinds.length > 0 && (
                <p className="mt-2 text-xs text-gray-500">
                    {kinds.map(([k, n]) => `${n} ${LOOSE_END_KIND_SHORT[k]}`).join(' · ')}
                </p>
            )}

            <AgingBar aging={person.aging} />

            <div className="mt-3 flex flex-wrap gap-1.5">
                {!person.coverage ? (
                    <span className="rounded-full border border-gray-300 bg-white px-2 py-0.5 text-xs text-gray-600">
                        Not scanned yet
                    </span>
                ) : gaps.length === 0 ? (
                    <span className="rounded-full border border-green-200 bg-green-50 px-2 py-0.5 text-xs text-green-800">
                        Every source read
                    </span>
                ) : (
                    gaps.map(g => (
                        <span
                            key={g.source}
                            className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs text-amber-900"
                            title={`${g.badge} on the last scan — nothing from it was counted or closed for this person.`}
                        >
                            {g.badge}
                        </span>
                    ))
                )}
            </div>

            {person.items.length > 0 && (
                <div className="mt-3">
                    <button
                        type="button"
                        className={LINK_BUTTON}
                        aria-expanded={open}
                        aria-controls={listId}
                        onClick={() => setOpen(o => !o)}
                    >
                        {open ? <ChevronDown size={15} aria-hidden="true" /> : <ChevronRight size={15} aria-hidden="true" />}
                        {open ? 'Hide the list' : `Show the ${plural(person.items.length, 'item')}`}
                    </button>
                    {open && (
                        <ul id={listId} className={`mt-2 ${LIST_CARD}`}>
                            {/* who / kind / age ONLY — the team payload has no
                                titles, headlines, subjects or snippets. */}
                            {person.items.map(item => (
                                <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5">
                                    <div className="min-w-0">
                                        <p className="truncate text-sm text-gray-900">{item.who || '—'}</p>
                                        <p className="text-xs text-gray-500">
                                            {isLooseEndKind(item.kind) ? LOOSE_END_KIND_LABELS[item.kind] : item.kind}
                                        </p>
                                    </div>
                                    {item.age && <AgeChip {...ageProps(item.age)} />}
                                </li>
                            ))}
                        </ul>
                    )}
                    {person.open > person.items.length && (
                        <p className="mt-1 text-xs text-gray-500">
                            The {person.items.length} oldest of {person.open} open.
                        </p>
                    )}
                </div>
            )}
        </div>
    );
};

const AgingBar: React.FC<{ aging: TeamPerson['aging'] }> = ({ aging }) => {
    const counts = AGING_BUCKETS.map(b => [b, aging?.[b] ?? 0] as const);
    const total = counts.reduce((n, [, c]) => n + c, 0);
    if (total === 0) return <p className="mt-3 text-xs text-gray-500">Nothing open.</p>;
    const summary = counts.map(([b, c]) => `${c} of ${total} at ${AGING_BUCKET_LABELS[b]}`).join(', ');
    return (
        <div className="mt-3">
            <div
                className="flex h-2.5 w-full overflow-hidden rounded-full bg-gray-100"
                role="img"
                aria-label={`Open items by age: ${summary}.`}
            >
                {counts.map(([b, c]) =>
                    c > 0 ? (
                        <div
                            key={b}
                            className={AGING_BAR_CLASSES[b]}
                            style={{ width: `${(c / total) * 100}%` }}
                            title={`${AGING_BUCKET_LABELS[b]}: ${c} of ${total}`}
                        />
                    ) : null,
                )}
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-500" aria-hidden="true">
                {counts.map(([b, c]) => (
                    <span key={b} className="inline-flex items-center gap-1">
                        <span className={`inline-block h-2 w-2 rounded-sm ${AGING_BAR_CLASSES[b]}`} />
                        {AGING_BUCKET_LABELS[b]}: {c} of {total}
                    </span>
                ))}
            </div>
        </div>
    );
};
