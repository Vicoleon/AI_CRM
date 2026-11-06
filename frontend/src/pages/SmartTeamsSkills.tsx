// ST-8 — My skills.
//
// FOUR BUCKETS, AND THE PAGE MUST NOT BLUR TWO OF THEM
// ----------------------------------------------------
// `GET /skills` returns `declared`, `accepted_suggestions`, `suggested` and
// `denied`. The first two are confirmed and colleague-visible; the third is a
// question Cubie asked and nobody else can see; the fourth is a decision that
// stuck. The failure this screen exists to avoid is the second and third being
// rendered alike — a person seeing Cubie's guess presented as their own claim,
// and then a colleague staffing them on it. So a suggestion is a separate
// section, a separate card shape, a separate colour, and says on its own face
// that it is not on the profile.
//
// EVERY WRITE REPAINTS FROM ITS OWN RESPONSE
// ------------------------------------------
// Declare, confirm and deny all return the WHOLE refreshed own-skills payload
// (`DeclareSkillsResponse` and `SkillMutationResponse` both extend
// `OwnSkillsResponse`). So there is no local mutation of a list and no re-fetch
// after a write: one response repaints the page, which is why a confirm can
// never leave a row in two buckets at once.
//
// THE REFRESH IS FIRE-AND-FORGET, AND THE COPY SAYS SO
// ---------------------------------------------------
// `POST /skills/refresh` is a 202 that dispatches the sweep on cubie-worker.
// There is no per-run poll and no progress — the sweep reads transcripts and
// makes a Vertex call, so it cannot run in the request. This page therefore does not spin: it says the look is happening in
// the background and offers an explicit "check for new ones", which is a plain
// re-read of `GET /skills`.
//
// Company-wide, like the interview and your own guide — not gated on core
// access. The route in App.tsx carries `SmartTeamsRoute`, and this page adds no
// gate of its own beyond rendering the 403 the API returns.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Lock, RefreshCw, Settings2, Wrench } from 'lucide-react';

import { useAuth } from '../context/AuthContext';

import { confirmDialog } from '../components/eos/ConfirmDialog';
import { toast } from '../components/eos/Toast';
import { EmptyState, ErrorState, LoadingState } from './eos/_shared';
import { AdvisoryNotice, SecondaryButton } from '../components/smartTeams/shared';
import { ColleagueSkills } from '../components/smartTeams/skills/ColleagueSkills';
import { ConfirmedSkills } from '../components/smartTeams/skills/ConfirmedSkills';
import { DeclareSkills } from '../components/smartTeams/skills/DeclareSkills';
import type { StagedSkill } from '../components/smartTeams/skills/DeclareSkills';
import { DeniedSkills } from '../components/smartTeams/skills/DeniedSkills';
import { SuggestionQueue } from '../components/smartTeams/skills/SuggestionQueue';
import type {
    ColleagueSkillsResponse,
    OwnSkillsResponse,
    PrivateSkill,
    SkillLevel,
} from '../api/smartTeams';
import {
    confirmOwnSkill,
    declareOwnSkills,
    denyOwnSkill,
    getColleagueSkills,
    getOwnSkills,
    refreshOwnSuggestions,
    smartTeamsError,
} from '../api/smartTeams';

/** What a declare actually did, kept on screen rather than in a toast: `undenied`
 *  means an earlier "no" was lifted, and that is worth reading. */
interface DeclareOutcome {
    added: number;
    updated: number;
    unchanged: number;
    undenied: string[];
}

export const SmartTeamsSkills: React.FC = () => {
    const { canViewSmartTeamsRawData } = useAuth();
    const [own, setOwn] = useState<OwnSkillsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [denied, setDenied] = useState(false);
    const [reloading, setReloading] = useState(false);

    // Per-skill in-flight set, so answering one suggestion does not disable the
    // other five.
    const [busyKeys, setBusyKeys] = useState<Set<string>>(new Set());

    const [staged, setStaged] = useState<Record<string, StagedSkill>>({});
    const [submitting, setSubmitting] = useState(false);
    const [submitError, setSubmitError] = useState<string | null>(null);
    const [outcome, setOutcome] = useState<DeclareOutcome | null>(null);

    const [refreshing, setRefreshing] = useState(false);
    const [refreshDispatched, setRefreshDispatched] = useState(false);

    const [colleague, setColleague] = useState<ColleagueSkillsResponse | null>(null);
    const [colleagueLoading, setColleagueLoading] = useState(false);
    const [colleagueError, setColleagueError] = useState<string | null>(null);

    const declareRef = useRef<HTMLDivElement | null>(null);

    const load = useCallback(async (): Promise<void> => {
        try {
            const next = await getOwnSkills();
            setOwn(next);
            setError(null);
            setDenied(false);
        } catch (e) {
            const err = smartTeamsError(e, 'Cubie could not load your skills.');
            // Only a 401 is handled by the axios interceptor's redirect, so the
            // 403 state is ours to render.
            if (err.status === 403) setDenied(true);
            else setError(err.message);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const reload = useCallback(async (): Promise<void> => {
        setReloading(true);
        try {
            setOwn(await getOwnSkills());
            setError(null);
        } catch (e) {
            toast.error(smartTeamsError(e, 'Cubie could not refresh your skills.').message);
        } finally {
            setReloading(false);
        }
    }, []);

    const markBusy = useCallback((key: string, busy: boolean) => {
        setBusyKeys(current => {
            const next = new Set(current);
            if (busy) next.add(key);
            else next.delete(key);
            return next;
        });
    }, []);

    const confirmed = useMemo<PrivateSkill[]>(
        () => (own ? [...own.declared, ...own.accepted_suggestions] : []),
        [own],
    );
    const confirmedKeys = useMemo(
        () => new Set(confirmed.map(skill => skill.skill_key)),
        [confirmed],
    );
    const deniedKeys = useMemo(
        () => new Set((own?.denied ?? []).map(skill => skill.skill_key)),
        [own],
    );

    // ── Answering a suggestion ───────────────────────────────────────────────

    const onConfirmSuggestion = useCallback(
        async (skillKey: string) => {
            markBusy(skillKey, true);
            try {
                const next = await confirmOwnSkill(skillKey);
                setOwn(next);
                toast.success(`${next.skill.name} is on your profile.`);
            } catch (e) {
                // 404 means the suggestion is no longer there — most likely
                // answered in another tab. Say that rather than "failed".
                const err = smartTeamsError(e, 'Cubie could not record that.');
                toast.error(
                    err.status === 404
                        ? 'That suggestion is no longer waiting for an answer. Reloading your skills.'
                        : err.message,
                );
                if (err.status === 404) await reload();
            } finally {
                markBusy(skillKey, false);
            }
        },
        [markBusy, reload],
    );

    /** No. One click, and it sticks — the server writes a tombstone the sweep
     *  reads, so there is no dialog here: this is an answer to a question, and
     *  the section it sits in already says the answer is durable. */
    const onDenySuggestion = useCallback(
        async (skillKey: string) => {
            markBusy(skillKey, true);
            try {
                const next = await denyOwnSkill(skillKey);
                setOwn(next);
                toast.success(`Noted — Cubie won't suggest ${next.skill.name} again.`);
            } catch (e) {
                toast.error(smartTeamsError(e, 'Cubie could not record that.').message);
            } finally {
                markBusy(skillKey, false);
            }
        },
        [markBusy],
    );

    /** Taking a confirmed skill off the profile. Same endpoint as a denial —
     *  there is no delete, because a delete would let the next sweep read the
     *  same meeting and suggest it again. The dialog says both halves of what
     *  actually happens. */
    const onRemoveConfirmed = useCallback(
        async (skill: PrivateSkill) => {
            const ok = await confirmDialog({
                title: `Take “${skill.name}” off your profile?`,
                message:
                    'Colleagues stop seeing it straight away, and Cubie will not suggest it to you again. You can add it back yourself at any time — nothing is deleted.',
                destructive: true,
            });
            if (!ok) return;
            markBusy(skill.skill_key, true);
            try {
                const next = await denyOwnSkill(skill.skill_key);
                setOwn(next);
                toast.success(`${next.skill.name} is off your profile.`);
            } catch (e) {
                toast.error(smartTeamsError(e, 'Cubie could not update that.').message);
            } finally {
                markBusy(skill.skill_key, false);
            }
        },
        [markBusy],
    );

    // ── Declaring ────────────────────────────────────────────────────────────

    const onStage = useCallback((skillKey: string, name: string, level: SkillLevel | null) => {
        setStaged(current => ({ ...current, [skillKey]: { name, level } }));
    }, []);

    const onUnstage = useCallback((skillKey: string) => {
        setStaged(current => {
            const next = { ...current };
            delete next[skillKey];
            return next;
        });
    }, []);

    /** From the denied list: stage it and take the person to the tray, so the
     *  lift is something they submit rather than something a button did. */
    const onReconsider = useCallback(
        (skillKey: string) => {
            const row = (own?.denied ?? []).find(skill => skill.skill_key === skillKey);
            if (!row) return;
            onStage(skillKey, row.name, row.level);
            declareRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        },
        [onStage, own],
    );

    const onSubmitDeclare = useCallback(async () => {
        const entries = Object.entries(staged);
        if (entries.length === 0) return;
        setSubmitting(true);
        setSubmitError(null);
        try {
            const body = {
                skills: Object.fromEntries(
                    entries.map(([key, entry]) => [key, entry.level]),
                ) as Record<string, SkillLevel | null>,
            };
            const next = await declareOwnSkills(body);
            setOwn(next);
            setOutcome({
                added: next.created.length,
                updated: next.updated.length,
                unchanged: next.unchanged.length,
                undenied: next.undenied,
            });
            setStaged({});
        } catch (e) {
            // The whole batch was rejected and nothing was written. The detail is
            // a sentence written for the person (an unknown key, a level that is
            // not a level) and names the path forward, so it is shown verbatim.
            setSubmitError(
                smartTeamsError(e, 'Cubie could not add those skills.').message,
            );
        } finally {
            setSubmitting(false);
        }
    }, [staged]);

    // ── A fresh look at my own activity ──────────────────────────────────────

    const onRefresh = useCallback(async () => {
        setRefreshing(true);
        try {
            await refreshOwnSuggestions();
            setRefreshDispatched(true);
        } catch (e) {
            toast.error(
                smartTeamsError(e, 'Cubie could not start a fresh look.').message,
            );
        } finally {
            setRefreshing(false);
        }
    }, []);

    // ── Looking somebody up ──────────────────────────────────────────────────

    const onLookup = useCallback(async (email: string) => {
        setColleagueLoading(true);
        setColleagueError(null);
        setColleague(null);
        try {
            setColleague(await getColleagueSkills(email));
        } catch (e) {
            const err = smartTeamsError(e, 'Cubie could not look that person up.');
            setColleagueError(
                err.status === 404
                    ? `Cubie has no colleague at ${email}. Check the spelling — it needs to be their work email address.`
                    : err.message,
            );
        } finally {
            setColleagueLoading(false);
        }
    }, []);

    // ── Render ───────────────────────────────────────────────────────────────

    if (loading) {
        return (
            <div className="mx-auto max-w-4xl p-8">
                <LoadingState label="Loading your skills…" />
            </div>
        );
    }

    if (denied) {
        return (
            <div className="mx-auto max-w-4xl p-8">
                <EmptyState
                    title="Smart Teams isn't switched on for this account"
                    hint="It's open to everyone with a work account at your company. If that's you and you're seeing this, tell your Cubie admin."
                />
            </div>
        );
    }

    if (error || !own) {
        return (
            <div className="mx-auto max-w-4xl space-y-3 p-8">
                <ErrorState message={error ?? 'Nothing came back.'} />
                <SecondaryButton onClick={() => void load()}>
                    <RefreshCw size={14} />
                    Try again
                </SecondaryButton>
            </div>
        );
    }

    return (
        <div className="mx-auto max-w-4xl p-8">
            <header data-tour="smart-teams-skills-header" className="mb-6 flex items-start justify-between gap-4">
                <div className="flex items-start gap-3">
                    <div className="flex items-center justify-center rounded-md bg-brand p-2.5 text-white">
                        <Wrench size={20} />
                    </div>
                    <div>
                        <h1 className="text-2xl font-semibold text-gray-900">My skills</h1>
                        <p className="mt-1 text-sm text-gray-500">
                            What you would be happy to be asked about on a project — and what Cubie
                            thinks you might be, for you to answer.
                        </p>
                    </div>
                </div>
                {/* Support admins curate the shared vocabulary. Reusing the
                    raw-data gate rather than adding a second mirrored constant:
                    it resolves to the same principals (admin role AND the support row
                    membership) and the server is the authority either way. This
                    only decides whether to show the link. */}
                {canViewSmartTeamsRawData && (
                    <Link
                        to="/smart-teams/skills/curate"
                        className="flex flex-shrink-0 items-center gap-1.5 rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 transition hover:border-brand/40 hover:text-brand"
                    >
                        <Settings2 size={14} />
                        Curate the list
                    </Link>
                )}
            </header>

            <div className="space-y-4">
                <SuggestionQueue
                    suggestions={own.suggested}
                    notice={own.suggestion_notice}
                    busyKeys={busyKeys}
                    onConfirm={key => void onConfirmSuggestion(key)}
                    onDeny={key => void onDenySuggestion(key)}
                    onRefresh={() => void onRefresh()}
                    refreshing={refreshing}
                    refreshDispatched={refreshDispatched}
                    onReload={() => void reload()}
                    reloading={reloading}
                />

                <ConfirmedSkills
                    skills={confirmed}
                    busyKeys={busyKeys}
                    onRemove={skill => void onRemoveConfirmed(skill)}
                />

                {/* What the last declare actually did, on screen rather than in a
                    toast that slides away — `undenied` in particular is a
                    reversal of the person's own earlier "no" and they are
                    entitled to see it said. */}
                {outcome && (
                    <div
                        role="status"
                        className="flex items-start justify-between gap-3 rounded-lg border border-green-200 bg-green-50 p-3"
                    >
                        <div className="text-xs leading-relaxed text-green-900">
                            <p className="font-medium">
                                {outcome.added > 0
                                    ? `${outcome.added} ${outcome.added === 1 ? 'skill' : 'skills'} added to your profile.`
                                    : 'Your profile is up to date.'}
                                {outcome.updated > 0 &&
                                    ` ${outcome.updated} ${outcome.updated === 1 ? 'level' : 'levels'} changed.`}
                                {outcome.unchanged > 0 &&
                                    ` ${outcome.unchanged} ${outcome.unchanged === 1 ? 'was' : 'were'} already exactly that.`}
                            </p>
                            {outcome.undenied.length > 0 && (
                                <p className="mt-1">
                                    You had previously said no to{' '}
                                    {outcome.undenied.length === 1
                                        ? 'one of these'
                                        : `${outcome.undenied.length} of these`}
                                    . Adding it yourself lifts that, so it is on your profile now
                                    — Cubie still will not suggest it to you unprompted.
                                </p>
                            )}
                        </div>
                        <button
                            type="button"
                            onClick={() => setOutcome(null)}
                            className="flex-shrink-0 rounded text-xs font-medium text-green-800 underline hover:no-underline"
                        >
                            Dismiss
                        </button>
                    </div>
                )}

                <div data-tour="smart-teams-skills-declare" ref={declareRef}>
                    <DeclareSkills
                        confirmedKeys={confirmedKeys}
                        deniedKeys={deniedKeys}
                        staged={staged}
                        onStage={onStage}
                        onUnstage={onUnstage}
                        onSubmit={() => void onSubmitDeclare()}
                        submitting={submitting}
                        submitError={submitError}
                    />
                </div>

                <DeniedSkills
                    skills={own.denied}
                    notice={own.denial_notice}
                    onReconsider={onReconsider}
                />

                <ColleagueSkills
                    onLookup={email => void onLookup(email)}
                    loading={colleagueLoading}
                    error={colleagueError}
                    result={colleague}
                />

                {/* No privacy notice is sent on this surface, so this is UI copy —
                    and every clause in it is what the router enforces rather than
                    a promise written here. Colleague reads go through
                    `public_dict`, whose DTO has no evidence field; the raw read is
                    `authz.can_view_raw_profile`, which grants exactly the person
                    themselves and a support admin (an exec is refused, a team
                    manager is refused). */}
                <div className="flex items-start gap-2.5 rounded-lg border border-gray-200 bg-gray-50 p-3">
                    <Lock className="mt-0.5 flex-shrink-0 text-gray-500" size={16} />
                    <div className="space-y-1 text-xs leading-relaxed text-gray-600">
                        <p>
                            What Cubie read to make a suggestion is visible to you and to a
                            support admin helping you — not to your manager and not to the Exec
                            team. Colleagues see only the skills you have confirmed, with the
                            level, and nothing about why.
                        </p>
                        <p>
                            Only you can confirm, add or remove your own skills. Nobody can do it
                            on your behalf, support included.
                        </p>
                    </div>
                </div>

                {/* Verbatim from the server — the same advisory promise every other
                    Smart Teams surface makes. */}
                <AdvisoryNotice text={own.advisory_notice} />
            </div>
        </div>
    );
};

export default SmartTeamsSkills;
