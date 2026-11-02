// Support: raw profile lookup.
//
// WHY THIS SCREEN EXISTS
// ----------------------
// Five support endpoints have been live and enforcing since ST-3 and had no UI
// at all, so the only way to answer "why is their guide stuck" was curl. The
// Smart Teams landing page advertised the gap as "coming soon" to exactly the
// people who needed it.
//
// WHAT IT IS ALLOWED TO BE
// ------------------------
// A support admin can READ everything here and CHANGE nothing. That is not an
// oversight in the routers, it is the design: approving a guide, confirming a
// merge, answering a skill suggestion and declaring a skill are all statements
// a person makes about themselves, and every one of those routes 403s a support
// admin on purpose. So this page has no action buttons, and the findings say who
// the ball is with rather than offering to move it.
//
// It also does not lead with the raw answers. The question people arrive with is
// about STATE — where are they stuck, what is waiting on whom — and answering it
// does not require reading what somebody typed about how they handle conflict.
// The answers are behind a disclosure that says how many there are, so opening
// them is a decision rather than a scroll.
//
// GATING
// ------
// `can_view_raw_profile`: the subject themselves, plus a support admin (the
// admin role AND the `smart_teams_support` row in Admin → Feature Access). An
// exec is refused, a team manager is refused, an admin the row leaves out is
// refused. The route carries only the ordinary Smart Teams gate and
// this page renders the server's 403 — a client-side check here would be a
// second opinion, and the server is the authority. `canViewSmartTeamsRawData`
// decides whether the LINK is shown, nothing more.
//
// Every read below is logged server-side against the caller. That is stated on
// screen, because a support tool whose audit trail is invisible invites people
// to treat a look as free.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
    AlertTriangle,
    ChevronDown,
    ChevronRight,
    CircleCheck,
    CircleDashed,
    FileText,
    Hourglass,
    RefreshCw,
    ShieldCheck,
    Upload,
    UserRound,
    Wrench,
} from 'lucide-react';

import ColleaguePicker from '../components/smartTeams/compare/ColleaguePicker';
import { useAuth } from '../context/AuthContext';
import { formatInstant } from '../utils/datetime';
import { ErrorState, LoadingState } from './eos/_shared';
import { AdvisoryNotice, SecondaryButton } from '../components/smartTeams/shared';
import {
    diagnose,
    headline,
    type Finding,
    type FindingTone,
    type SupportInterview,
} from '../components/smartTeams/support/diagnosis';
import type {
    AnswerValue,
    GuideDocument,
    PrivateSkill,
    SkillSupportResponse,
    UploadRecord,
} from '../api/smartTeams';
import {
    smartTeamsError,
    supportReadGuides,
    supportReadInterview,
    supportReadSkills,
    supportReadUploads,
} from '../api/smartTeams';

// ── Small presentational pieces ──────────────────────────────────────────────

const TONE_STYLE: Record<FindingTone, { box: string; icon: React.ReactNode; label: string }> = {
    blocked: {
        box: 'border-red-200 bg-red-50',
        icon: <AlertTriangle size={16} className="mt-0.5 flex-shrink-0 text-red-600" />,
        label: 'Broken',
    },
    attention: {
        box: 'border-amber-200 bg-amber-50',
        icon: <AlertTriangle size={16} className="mt-0.5 flex-shrink-0 text-amber-600" />,
        label: 'Worth a look',
    },
    waiting: {
        box: 'border-gray-200 bg-gray-50',
        icon: <Hourglass size={16} className="mt-0.5 flex-shrink-0 text-gray-500" />,
        label: 'Waiting',
    },
    ok: {
        box: 'border-green-200 bg-green-50',
        icon: <CircleCheck size={16} className="mt-0.5 flex-shrink-0 text-green-600" />,
        label: 'In order',
    },
};

const OWNER_LABEL: Record<NonNullable<Finding['owner']>, string> = {
    system: 'Needs a fix',
    them: 'Theirs to do',
    support: 'An admin can act',
};

const FindingCard: React.FC<{ finding: Finding }> = ({ finding }) => {
    const tone = TONE_STYLE[finding.tone];
    return (
        <li className={`flex items-start gap-2.5 rounded-lg border p-3.5 ${tone.box}`}>
            {tone.icon}
            <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-semibold text-gray-900">{finding.title}</h3>
                    {finding.owner && (
                        <span className="rounded-full bg-white/70 px-2 py-0.5 text-[11px] font-medium text-gray-600">
                            {OWNER_LABEL[finding.owner]}
                        </span>
                    )}
                </div>
                <p className="mt-1 text-sm leading-relaxed text-gray-700">{finding.detail}</p>
            </div>
        </li>
    );
};

/** A labelled block that can report its own read failure.
 *
 *  Per-panel rather than per-page because the four reads are independent: a
 *  skills sweep that 500s must not hide an interview that loaded fine. */
const Panel: React.FC<{
    icon: React.ReactNode;
    title: string;
    error?: string | null;
    children: React.ReactNode;
}> = ({ icon, title, error, children }) => (
    <section className="rounded-lg border border-gray-200 bg-white">
        <header className="flex items-center gap-2 border-b border-gray-100 px-5 py-3">
            <span className="text-brand">{icon}</span>
            <h2 className="text-sm font-semibold text-gray-900">{title}</h2>
        </header>
        <div className="px-5 py-4">
            {error ? (
                <p className="flex items-start gap-2 text-sm text-red-700">
                    <AlertTriangle size={15} className="mt-0.5 flex-shrink-0" />
                    {error}
                </p>
            ) : (
                children
            )}
        </div>
    </section>
);

/** `label: value` rows. `—` for anything absent, never a blank cell. */
const Facts: React.FC<{ rows: [string, React.ReactNode][] }> = ({ rows }) => (
    <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {rows.map(([label, value]) => (
            <div key={label} className="min-w-0">
                <dt className="text-xs font-medium uppercase tracking-wide text-gray-400">
                    {label}
                </dt>
                <dd className="mt-0.5 break-words text-sm text-gray-900">{value ?? '—'}</dd>
            </div>
        ))}
    </dl>
);

const stamp = (iso: string | null | undefined): string => formatInstant(iso);

// ── Raw answers ──────────────────────────────────────────────────────────────

/** One stored answer, printed as what it IS.
 *
 *  The five shapes are genuinely different — a scale number, an a/b forced
 *  choice, a skill level, a whole scripted conversation, the Enneagram
 *  confirmation object — and `JSON.stringify` on all of them would turn the two
 *  that matter most into unreadable one-liners. `null` is printed as "cleared",
 *  which is distinct from a key that was never asked: the interview treats those
 *  differently and so must anything debugging it. */
const AnswerCell: React.FC<{ value: AnswerValue }> = ({ value }) => {
    if (value === null || value === undefined) {
        return <span className="italic text-gray-400">cleared</span>;
    }
    if (typeof value === 'number' || typeof value === 'string') {
        return <span className="font-mono text-gray-900">{String(value)}</span>;
    }
    if (Array.isArray(value)) {
        return (
            <div className="space-y-1.5">
                {value.map((turn, i) => (
                    <p key={i} className="text-gray-700">
                        <span
                            className={
                                turn.speaker === 'cubie'
                                    ? 'font-medium text-gray-400'
                                    : 'font-medium text-brand'
                            }
                        >
                            {turn.speaker === 'cubie' ? 'Cubie' : 'They'}:{' '}
                        </span>
                        {turn.text}
                    </p>
                ))}
            </div>
        );
    }
    // The Enneagram confirmation object.
    return (
        <span className="font-mono text-gray-900">
            {value.declined
                ? 'declined'
                : `type ${value.type_number ?? '—'}${value.wing ? `w${value.wing}` : ''}`}
        </span>
    );
};

const RawAnswers: React.FC<{ answers: Record<string, AnswerValue> }> = ({ answers }) => {
    const [open, setOpen] = useState(false);
    const keys = useMemo(() => Object.keys(answers).sort(), [answers]);

    if (!keys.length) {
        return <p className="text-sm text-gray-500">No answers stored on this interview.</p>;
    }

    return (
        <div>
            <button
                type="button"
                onClick={() => setOpen(o => !o)}
                aria-expanded={open}
                className="flex items-center gap-1.5 rounded text-sm font-medium text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
            >
                {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                {open ? 'Hide' : 'Show'} the {keys.length} stored answers
            </button>
            <p className="mt-1 text-xs leading-relaxed text-gray-500">
                What they typed and chose, in their words. Read it with them, and only when
                the state above has not answered the question.
            </p>
            {open && (
                <dl className="mt-3 divide-y divide-gray-100 rounded-md border border-gray-200">
                    {keys.map(key => (
                        <div key={key} className="grid gap-1 px-3 py-2 sm:grid-cols-[minmax(0,14rem)_1fr] sm:gap-4">
                            <dt className="break-all font-mono text-xs text-gray-500">{key}</dt>
                            <dd className="text-sm">
                                <AnswerCell value={answers[key]} />
                            </dd>
                        </div>
                    ))}
                </dl>
            )}
        </div>
    );
};

// ── Skills ───────────────────────────────────────────────────────────────────

const SkillList: React.FC<{ title: string; skills: PrivateSkill[]; hint?: string }> = ({
    title,
    skills,
    hint,
}) => (
    <div>
        <h3 className="text-xs font-medium uppercase tracking-wide text-gray-400">
            {title} ({skills.length})
        </h3>
        {hint && <p className="mt-0.5 text-xs text-gray-500">{hint}</p>}
        {skills.length === 0 ? (
            <p className="mt-1 text-sm text-gray-500">None.</p>
        ) : (
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
                {skills.map(skill => (
                    <li
                        key={skill.skill_key}
                        title={skill.rationale || undefined}
                        className="rounded-full border border-gray-200 bg-gray-50 px-2.5 py-1 text-xs text-gray-700"
                    >
                        {skill.name}
                        {skill.level && <span className="text-gray-400"> · {skill.level}</span>}
                        {skill.evidence.length > 0 && (
                            <span className="text-gray-400"> · {skill.evidence.length} cited</span>
                        )}
                    </li>
                ))}
            </ul>
        )}
    </div>
);

// ── Page ─────────────────────────────────────────────────────────────────────

type ReadKey = 'interview' | 'guides' | 'skills' | 'uploads';
type ReadErrors = Record<ReadKey, string | null>;

interface Loaded {
    /** Whose payloads these are. Carried on the state rather than assumed,
     *  so switching subjects cannot render person A's findings under person
     *  B's address while B's four reads are still in flight. */
    forEmail: string;
    interview: SupportInterview | null;
    guides: GuideDocument[] | null;
    skills: SkillSupportResponse | null;
    uploads: UploadRecord[] | null;
    errors: ReadErrors;
}

/** Annotated, not inferred: without the type every value widens to `null` and
 *  the spread below cannot take a message. */
const EMPTY_ERRORS: ReadErrors = {
    interview: null,
    guides: null,
    skills: null,
    uploads: null,
};

export const SmartTeamsSupport: React.FC = () => {
    const { user } = useAuth();
    // In the URL so a lookup can be pasted into a ticket and reopened, and so a
    // refresh does not drop the admin back on the picker mid-call.
    const [params, setParams] = useSearchParams();
    const subject = (params.get('email') ?? '').trim();

    const [loaded, setLoaded] = useState<Loaded | null>(null);
    // Only the MANUAL refresh needs a stored flag. The first load's spinner is
    // derived below from "there is a subject and no settled result for it yet",
    // which is both true by construction and the only shape that keeps the
    // effect free of a synchronous setState (`react-hooks/set-state-in-effect` —
    // the effect re-runs on every subject change, so a sync set here really
    // would be a cascading render, not a false positive).
    const [refreshing, setRefreshing] = useState(false);
    // Carries whose refusal it is, for the same reason `Loaded` does: a 403 on
    // one address must not still be on screen while a different one loads.
    const [denial, setDenial] = useState<{ forEmail: string; message: string } | null>(null);
    /** The address whose reads are allowed to write state. See `load`. */
    const latest = useRef('');

    // Closes over `subject` and guards inside, rather than taking it as an
    // argument the effect passes in. Same shape as every other page here, and
    // the shape `react-hooks/set-state-in-effect` accepts: the effect body is a
    // bare call, and the state this sets belongs to the fetch it is starting.
    const load = useCallback(async () => {
        const email = subject;
        if (!email) return;
        // Whose lookup is current. Twelve requests can be in flight across two
        // subjects if somebody switches while the first is still running, and
        // without this the SLOWER one wins the last write: state would end up
        // stamped for a person who is no longer in the URL, `data` would derive
        // to null, and the page would sit on the spinner forever.
        latest.current = email;

        // All four in parallel, and each settled on its own: one failing read is
        // a note in its panel, not a dead page. A 403 is different — it is the
        // same gate on all four, so it is the whole answer and replaces the page.
        const [interview, guides, skills, uploads] = await Promise.allSettled([
            supportReadInterview(email),
            supportReadGuides(email),
            supportReadSkills(email),
            supportReadUploads(email),
        ]);
        if (latest.current !== email) return; // superseded

        const forbidden = [interview, guides, skills, uploads].find(
            r => r.status === 'rejected' && smartTeamsError(r.reason).status === 403,
        );
        if (forbidden && forbidden.status === 'rejected') {
            setDenial({ forEmail: email, message: smartTeamsError(forbidden.reason).message });
            setLoaded(null);
            return;
        }

        const errors: ReadErrors = { ...EMPTY_ERRORS };
        const unwrap = <T,>(
            result: PromiseSettledResult<T>,
            key: ReadKey,
            fallback: string,
        ): T | null => {
            if (result.status === 'fulfilled') return result.value;
            errors[key] = smartTeamsError(result.reason, fallback).message;
            return null;
        };

        // Unwrapped BEFORE the object literal, not inside it. `unwrap` mutates
        // `errors` as a side effect, so building the literal inline would make
        // the panels' error messages depend on `errors` being the last key —
        // a reorder anybody would call cosmetic would silently empty them.
        const interviewData = unwrap(
            interview,
            'interview',
            'Could not read their interview.',
        ) as SupportInterview | null;
        const guideRows = unwrap(guides, 'guides', 'Could not read their guide history.');
        const skillData = unwrap(skills, 'skills', 'Could not read their skills.');
        const uploadRows = unwrap(uploads, 'uploads', 'Could not read their uploads.');

        setDenial(null);
        setLoaded({
            forEmail: email,
            interview: interviewData,
            guides: guideRows?.guides ?? null,
            skills: skillData,
            uploads: uploadRows?.uploads ?? null,
            errors,
        });
    }, [subject]);

    useEffect(() => {
        void load();
    }, [load]);

    /** The manual re-read. Its flag is set from a click, not from an effect. */
    const refresh = useCallback(async () => {
        setRefreshing(true);
        try {
            await load();
        } finally {
            setRefreshing(false);
        }
    }, [load]);

    // Derived, not cleared in the effect above. Two things fall out of this:
    // removing the subject shows the picker with no extra setState, and a
    // half-finished switch from one person to another renders the loading state
    // instead of the previous person's findings under the new person's address.
    const data = loaded && loaded.forEmail === subject ? loaded : null;
    const denied = denial && denial.forEmail === subject ? denial.message : null;
    /** No settled result for the address in the URL yet. */
    const busy = !!subject && !data && !denied;

    const pick = useCallback(
        (email: string) => setParams({ email: email.trim().toLowerCase() }, { replace: false }),
        [setParams],
    );

    const findings = useMemo(() => {
        if (!data) return [];
        return diagnose({
            interview: data.interview,
            guides: data.guides,
            uploads: data.uploads,
            pendingSuggestions: data.skills?.suggested.length ?? null,
            // Read at render. The page is not long-lived and every finding is
            // day-grained, so a ticking clock would buy nothing.
            now: new Date(),
        });
    }, [data]);

    const verdict = useMemo(() => headline(findings), [findings]);
    const interview = data?.interview;
    const scored =
        interview && interview.status !== 'not_started' ? interview.profile : null;

    return (
        <div className="mx-auto max-w-4xl px-4 py-8">
            <header data-tour="smart-teams-support-header" className="mb-5 flex items-start gap-3">
                <div className="flex items-center justify-center rounded-md bg-brand p-2.5 text-white">
                    <ShieldCheck size={20} />
                </div>
                <div>
                    <h1 className="text-2xl font-semibold text-gray-900">Raw profile lookup</h1>
                    <p className="mt-1 text-sm text-gray-500">
                        Support view: where one person actually is, and what is holding them
                        up.
                    </p>
                </div>
            </header>

            <div data-tour="smart-teams-support-notice" className="mb-6 flex items-start gap-2.5 rounded-lg border border-gray-200 bg-gray-50 p-4">
                <ShieldCheck size={16} className="mt-0.5 flex-shrink-0 text-gray-500" />
                <p className="text-sm leading-relaxed text-gray-700">
                    This shows one person&apos;s private answers and scores, which nobody else
                    in the company can see — not their manager, not the Exec team. It is for
                    debugging a problem <span className="font-medium">with</span> the person it
                    belongs to. Every lookup is logged against you. Nothing here can be
                    changed: approving a guide, confirming an upload and answering a skill
                    suggestion are all theirs alone.
                </p>
            </div>

            {!subject && (
                <ColleaguePicker
                    selfEmail={user?.email ?? ''}
                    allowSelf
                    inputId="support-subject-search"
                    label="Whose profile are you debugging?"
                    hint="Search the company directory, or type a full work email address. Looking up your own is fine — it is the safest way to see what this screen shows."
                    onPick={pick}
                />
            )}

            {subject && (
                <>
                    <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white px-5 py-3.5">
                        <div className="flex min-w-0 items-center gap-2.5">
                            <UserRound size={16} className="flex-shrink-0 text-gray-400" />
                            <span className="truncate font-mono text-sm text-gray-900">
                                {subject}
                            </span>
                        </div>
                        <div className="flex items-center gap-2">
                            <SecondaryButton onClick={() => void refresh()} busy={refreshing}>
                                <RefreshCw size={14} />
                                Refresh
                            </SecondaryButton>
                            <SecondaryButton onClick={() => setParams({})}>
                                Look up someone else
                            </SecondaryButton>
                        </div>
                    </div>

                    {denied && <ErrorState message={denied} />}

                    {busy && <LoadingState label="Reading their profile…" />}

                    {!denied && data && (
                        <div className="space-y-4">
                            {/* The verdict, and then the findings behind it. This
                                is the whole reason the screen is not four JSON
                                dumps — see `diagnosis.ts`. */}
                            <div
                                className={`rounded-lg border p-4 ${TONE_STYLE[verdict.tone].box}`}
                            >
                                <div className="flex items-start gap-2.5">
                                    {TONE_STYLE[verdict.tone].icon}
                                    <p className="text-sm font-medium text-gray-900">
                                        {verdict.text}
                                    </p>
                                </div>
                            </div>

                            {findings.length > 0 && (
                                <ul className="space-y-2">
                                    {findings.map(finding => (
                                        <FindingCard key={finding.id} finding={finding} />
                                    ))}
                                </ul>
                            )}

                            <Panel
                                icon={<CircleDashed size={16} />}
                                title="Interview"
                                error={data.errors.interview}
                            >
                                {!interview || interview.status === 'not_started' ? (
                                    <p className="text-sm text-gray-500">
                                        No interview row exists for them.
                                    </p>
                                ) : (
                                    <div className="space-y-4">
                                        <Facts
                                            rows={[
                                                ['Status', interview.status],
                                                [
                                                    'Progress',
                                                    `${Math.round(interview.progress.percent)}% · ${interview.progress.sections_done}/${interview.progress.sections_total} sections · ${interview.progress.steps_done}/${interview.progress.steps_total} steps`,
                                                ],
                                                ['Started', stamp(interview.started_at)],
                                                ['Last activity', stamp(interview.last_activity_at)],
                                                ['Completed', stamp(interview.completed_at)],
                                                [
                                                    'Resumes at',
                                                    interview.resume_section_key ?? '—',
                                                ],
                                                [
                                                    'First unanswered',
                                                    interview.first_unanswered_item ?? '—',
                                                ],
                                                [
                                                    'Interview id',
                                                    <span key="id" className="font-mono text-xs">
                                                        {interview.interview_id}
                                                    </span>,
                                                ],
                                            ]}
                                        />
                                        <div className="border-t border-gray-100 pt-4">
                                            <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-400">
                                                Scored profile
                                            </h3>
                                            {scored ? (
                                                <Facts
                                                    rows={[
                                                        ['Version', scored.version],
                                                        ['Status', scored.status],
                                                        ['Origin', scored.origin],
                                                        ['Scored at', stamp(scored.scored_at)],
                                                    ]}
                                                />
                                            ) : (
                                                <p className="text-sm text-gray-500">
                                                    No profile. Nothing downstream can work until
                                                    the scoring run lands.
                                                </p>
                                            )}
                                        </div>
                                        <div className="border-t border-gray-100 pt-4">
                                            <RawAnswers answers={interview.section_answers} />
                                        </div>
                                    </div>
                                )}
                            </Panel>

                            <Panel
                                icon={<FileText size={16} />}
                                title="Guide generations"
                                error={data.errors.guides}
                            >
                                {!data.guides?.length ? (
                                    <p className="text-sm text-gray-500">
                                        They have never generated a guide.
                                    </p>
                                ) : (
                                    <ul className="divide-y divide-gray-100">
                                        {data.guides.map(row => (
                                            <li key={row.id} className="py-2.5 first:pt-0 last:pb-0">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className="text-sm font-medium text-gray-900">
                                                        {row.status}
                                                    </span>
                                                    {row.published && (
                                                        <span className="rounded-full bg-green-100 px-2 py-0.5 text-[11px] font-medium text-green-800">
                                                            published
                                                        </span>
                                                    )}
                                                    {row.stale && (
                                                        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800">
                                                            stale
                                                        </span>
                                                    )}
                                                    {row.rag_ingested && (
                                                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-700">
                                                            in Chat
                                                        </span>
                                                    )}
                                                    <span className="text-xs text-gray-500">
                                                        v{row.profile_version ?? '—'} ·{' '}
                                                        {stamp(row.created_at)}
                                                    </span>
                                                </div>
                                                {row.error && (
                                                    <p className="mt-1 text-sm text-red-700">
                                                        {row.error}
                                                    </p>
                                                )}
                                            </li>
                                        ))}
                                    </ul>
                                )}
                                {/* Bodies are deliberately not served here. Say
                                    so, or the absence reads as a bug. */}
                                <p className="mt-3 border-t border-gray-100 pt-3 text-xs leading-relaxed text-gray-500">
                                    The API does not return guide text on this read — it answers
                                    &ldquo;why is it stuck&rdquo;, not &ldquo;what does it
                                    say&rdquo;. A published guide is readable the normal way, by
                                    anyone.
                                </p>
                            </Panel>

                            <Panel
                                icon={<Wrench size={16} />}
                                title="Skills"
                                error={data.errors.skills}
                            >
                                {!data.skills ? null : (
                                    <div className="space-y-4">
                                        <SkillList
                                            title="Declared"
                                            skills={data.skills.declared}
                                        />
                                        <SkillList
                                            title="Confirmed suggestions"
                                            skills={data.skills.accepted_suggestions}
                                        />
                                        <SkillList
                                            title="Unanswered suggestions"
                                            skills={data.skills.suggested}
                                            hint="Invisible to everyone else until they answer."
                                        />
                                        <SkillList
                                            title="Denied"
                                            skills={data.skills.denied}
                                            hint="Cubie will not suggest these again."
                                        />
                                    </div>
                                )}
                            </Panel>

                            <Panel
                                icon={<Upload size={16} />}
                                title="Official report uploads"
                                error={data.errors.uploads}
                            >
                                {!data.uploads?.length ? (
                                    <p className="text-sm text-gray-500">
                                        Nothing uploaded — which is normal. Almost nobody has an
                                        official report, and none is expected.
                                    </p>
                                ) : (
                                    <ul className="divide-y divide-gray-100">
                                        {data.uploads.map(row => (
                                            <li key={row.id} className="py-2.5 first:pt-0 last:pb-0">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className="text-sm font-medium text-gray-900">
                                                        {row.original_filename}
                                                    </span>
                                                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] font-medium text-gray-700">
                                                        {row.review_status}
                                                    </span>
                                                    <span className="text-xs text-gray-500">
                                                        {row.framework ?? 'unknown framework'} ·{' '}
                                                        {stamp(row.uploaded_at)}
                                                    </span>
                                                </div>
                                                {row.parse_error && (
                                                    <p className="mt-1 text-sm text-red-700">
                                                        {row.parse_error}
                                                    </p>
                                                )}
                                            </li>
                                        ))}
                                    </ul>
                                )}
                            </Panel>

                            <AdvisoryNotice
                                text={data.skills?.advisory_notice}
                                className="mt-2"
                            />
                        </div>
                    )}
                </>
            )}
        </div>
    );
};

export default SmartTeamsSupport;
