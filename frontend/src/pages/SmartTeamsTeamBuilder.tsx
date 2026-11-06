// Team Builder (ST-9). Gated on core access as well as Smart Teams.
//
// Ask for the skills a piece of work needs; get back one group that covers as
// much of the ask as anybody has confirmed, plus an honest account of what it
// could not cover and why.
//
// THE SHAPE OF THIS PAGE IS DICTATED BY WHAT THE ROUTE DOES NOT HAVE
// -----------------------------------------------------------------
//  * `POST /teams` is synchronous and there is NO GET and NO HISTORY. An answer
//    is not persisted anywhere, so this page holds it in state and says so — a
//    "saved teams" list would be a promise the API cannot keep.
//  * It is always 200 when the ask parses. `state: 'empty'` with `shortfalls` is
//    the truthful answer to an ask nobody satisfies, so an empty group renders as
//    an ANSWER (see `TeamAnswerView`), never as an error and never as a blank.
//  * A 400 is a bad ask and its `detail` is the contract's own sentence, written
//    for a reader — so it is rendered verbatim next to the form the person can
//    fix, not as a generic failure.
//  * A 403 is the core-access gate, which is on the router itself
//    (`require_full_app_access`). It renders as the access notice rather than
//    an error, because the client-side flags and the server's verdict can
//    disagree.
import React, { useState } from 'react';
import { AlertTriangle, RotateCcw, UsersRound } from 'lucide-react';

import {
    buildTeam,
    smartTeamsError,
    type TeamAnswerResponse,
    type TeamAskBody,
    type SmartTeamsError,
} from '../api/smartTeams';
import { useAuth } from '../context/AuthContext';
import { SecondaryButton } from '../components/smartTeams/shared';
import CopilotOnly from '../components/smartTeams/compare/CopilotOnly';
import TeamAnswerView from '../components/smartTeams/teams/TeamAnswerView';
import TeamAskForm from '../components/smartTeams/teams/TeamAskForm';

export const SmartTeamsTeamBuilder: React.FC = () => {
    const { canAccessSmartTeams, canAccessFullApp } = useAuth();

    const [answer, setAnswer] = useState<TeamAnswerResponse | null>(null);
    const [error, setError] = useState<SmartTeamsError | null>(null);
    const [submitting, setSubmitting] = useState(false);

    if (!canAccessSmartTeams || !canAccessFullApp) {
        return (
            <div className="mx-auto max-w-4xl p-6">
                <CopilotOnly surface="Team Builder" />
            </div>
        );
    }

    // A 403 from the router means the client-side access flag and the server
    // disagree. Say what the server said, in the shape a person can act on.
    if (error?.status === 403) {
        return (
            <div className="mx-auto max-w-4xl p-6">
                <CopilotOnly surface="Team Builder" refusal={error.message} />
            </div>
        );
    }

    const onSubmit = async (body: TeamAskBody) => {
        setSubmitting(true);
        setError(null);
        try {
            setAnswer(await buildTeam(body));
        } catch (e) {
            setError(smartTeamsError(e, 'Cubie could not answer that ask.'));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div className="mx-auto max-w-4xl p-6">
            <h1 data-tour="smart-teams-team-builder-header" className="mb-1 flex items-center gap-2 text-xl font-semibold text-gray-900">
                <UsersRound size={20} className="text-brand" aria-hidden="true" />
                Team Builder
            </h1>
            <p className="mb-5 text-sm leading-relaxed text-gray-600">
                Name the skills a piece of work needs and Cubie proposes one group that
                covers them. It never ranks or scores anybody, and it is advisory only —
                nothing here is used for performance review, promotion, compensation, or
                deciding whether someone is right for a role.
            </p>

            {!answer && (
                <TeamAskForm onSubmit={onSubmit} submitting={submitting} />
            )}

            {error && (
                <div
                    role="alert"
                    className="mt-4 rounded-xl border border-red-200 bg-red-50 p-5"
                >
                    <div className="flex items-start gap-2.5">
                        <AlertTriangle
                            size={18}
                            aria-hidden="true"
                            className="mt-0.5 flex-shrink-0 text-red-500"
                        />
                        <div>
                            <p className="text-sm font-medium text-red-900">
                                {error.message}
                            </p>
                            {error.status === 400 && (
                                <p className="mt-1 text-xs text-red-700">
                                    Change the ask above and try again — nothing was run.
                                </p>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {answer && (
                <div className="space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                        <p className="text-xs leading-relaxed text-gray-500">
                            This answer is not saved anywhere. Cubie works it out fresh
                            each time, so the same ask next week may come back different as
                            people confirm skills and book time off.
                        </p>
                        <SecondaryButton
                            onClick={() => {
                                setAnswer(null);
                                setError(null);
                            }}
                        >
                            <RotateCcw size={14} aria-hidden="true" />
                            Ask something else
                        </SecondaryButton>
                    </div>
                    <TeamAnswerView answer={answer} />
                </div>
            )}
        </div>
    );
};

export default SmartTeamsTeamBuilder;
