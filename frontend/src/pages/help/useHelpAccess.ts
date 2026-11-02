import { useMemo } from 'react';
import { useAuth } from '../../context/AuthContext';
import type { HelpAccess } from './helpTopics';

/**
 * The reader's verdict for every Help gate, from the same AuthContext booleans
 * that gate the features themselves (all of them the server's word, from
 * /api/users/me). Shared by the Help panel, the Help home and each topic page,
 * so the three can never disagree about which topics a reader sees.
 */
export const useHelpAccess = (): HelpAccess => {
    const {
        canAccessXray,
        canAccessCrmSync,
        canAccessScheduledTasks,
        canAccessTodos,
        canAccessProposals,
        hasFeature,
    } = useAuth();
    // No dedicated boolean in AuthContext: read the registry verdict by key.
    // `hasFeature` is a fresh closure every render, so the memo keys on the
    // boolean it returns, not on the function.
    const canUseVoiceCommands = hasFeature('voice_commands');
    const canAccessInsights = hasFeature('insights');
    // Off with the kill switch and under impersonation too: /api/users/me folds both in.
    const canSeeWhatsNew = hasFeature('whats_new');
    // Off with the kill switch too, for the same reason.
    const canUseChromeExtension = hasFeature('chrome_extension');
    return useMemo(
        () => ({
            xray: canAccessXray,
            crm_sync: canAccessCrmSync,
            scheduled_tasks: canAccessScheduledTasks,
            todos: canAccessTodos,
            proposals: canAccessProposals,
            voice_commands: canUseVoiceCommands,
            insights: canAccessInsights,
            whats_new: canSeeWhatsNew,
            chrome_extension: canUseChromeExtension,
        }),
        [
            canAccessXray,
            canAccessCrmSync,
            canAccessScheduledTasks,
            canAccessTodos,
            canAccessProposals,
            canUseVoiceCommands,
            canAccessInsights,
            canSeeWhatsNew,
            canUseChromeExtension,
        ],
    );
};
