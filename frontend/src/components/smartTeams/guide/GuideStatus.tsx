// Who can see this guide right now, and what is still in flight.
//
// THE TWO HALVES OF "PUBLISHED" ARE NEVER AVERAGED
// ------------------------------------------------
// Approving publishes the guide inside Cubie synchronously and dispatches the
// Chat corpus copy to cubie-worker; unpublishing withdraws it from the app
// synchronously and dispatches the purge. `rag_ingested` is the row's own value
// at read time, so:
//
//   published  + rag_ingested false → in the app, NOT yet quotable in Chat
//   published  + rag_ingested true  → both
//   retracted  + rag_ingested true  → out of the app, STILL quotable in Chat
//   retracted  + rag_ingested false → gone from both
//
// The third line is the one that matters and the one a single "unpublished"
// badge would hide. The router's docstring says the response reports both
// "rather than averaging them into 'unpublished'"; this component is the UI half
// of that promise.
//
// `stale` is `guide.is_stale` — server-decided, from an indexed comparison of
// the profile version key. It is never recomputed here from the two version
// numbers, because the server's answer is the one the Regenerate path acts on.
import React from 'react';
import { AlertTriangle, Eye, EyeOff, Loader2, Lock, RefreshCw } from 'lucide-react';

import type { GuideDocument } from '../../../api/smartTeams';
import { PrimaryButton } from '../shared';
import { ownGuidePhase, shortDate } from './guideState';

/** The Chat half of publication, said only when there is something to say. */
const CorpusLine: React.FC<{ guide: GuideDocument }> = ({ guide }) => {
    if (guide.published && !guide.rag_ingested) {
        return (
            <p className="mt-2 flex items-start gap-1.5 text-xs leading-relaxed text-gray-600">
                <Loader2 className="mt-0.5 flex-shrink-0 animate-spin" size={12} />
                Cubie is still copying it into Chat&apos;s knowledge base. Until that
                finishes, Chat cannot quote it — colleagues can already read it here.
            </p>
        );
    }
    if (guide.published && guide.rag_ingested) {
        return (
            <p className="mt-2 text-xs leading-relaxed text-gray-600">
                Cubie Chat can also quote it when somebody asks how to work with you.
            </p>
        );
    }
    if (!guide.published && guide.rag_ingested) {
        return (
            <p className="mt-2 flex items-start gap-1.5 text-xs leading-relaxed text-amber-800">
                <AlertTriangle className="mt-0.5 flex-shrink-0" size={12} />
                It is out of the app already, but Cubie has not finished removing the Chat
                copy — for a short while Chat may still quote it. Nobody can open the
                document itself.
            </p>
        );
    }
    return null;
};

/** The banner at the top of the subject's own guide: exactly who can read it. */
export const OwnVisibilityBanner: React.FC<{ guide: GuideDocument }> = ({ guide }) => {
    const phase = ownGuidePhase(guide);
    const approved = shortDate(guide.approved_at);
    const retracted = shortDate(guide.unpublished_at);

    if (phase === 'published') {
        return (
            <div className="rounded-lg border border-green-200 bg-green-50 p-4">
                <p className="flex items-center gap-2 text-sm font-medium text-green-900">
                    <Eye size={16} />
                    Everyone at your company can read this guide.
                </p>
                <p className="mt-1 text-xs leading-relaxed text-green-800">
                    You published it{approved ? ` on ${approved}` : ''}. You can take it back
                    out of company view at any time, and colleagues lose access the moment you
                    do.
                </p>
                <CorpusLine guide={guide} />
            </div>
        );
    }

    if (phase === 'retracted') {
        return (
            <div className="rounded-lg border border-gray-300 bg-gray-50 p-4">
                <p className="flex items-center gap-2 text-sm font-medium text-gray-900">
                    <EyeOff size={16} className="text-gray-500" />
                    This guide is out of company view.
                </p>
                <p className="mt-1 text-xs leading-relaxed text-gray-600">
                    You published it{approved ? ` on ${approved}` : ''} and took it out of
                    company view{retracted ? ` on ${retracted}` : ''}. No colleague can open
                    it now. Publishing it again is yours to decide, and nothing about it has
                    been deleted.
                </p>
                <CorpusLine guide={guide} />
            </div>
        );
    }

    return (
        <div className="rounded-lg border border-gray-300 bg-gray-50 p-4">
            <p className="flex items-center gap-2 text-sm font-medium text-gray-900">
                <Lock size={16} className="text-gray-500" />
                This is a draft. It is not company-visible.
            </p>
            {/* Accurate to `authz.can_view_guide`, which grants an UNPUBLISHED
                draft to its subject AND to a support admin — they can already see
                the raw answers it was written from, so hiding the draft would
                protect nothing and make a support request unanswerable. Claiming
                "nobody but you" would be the easier sentence and a false one. */}
            <p className="mt-1 text-xs leading-relaxed text-gray-600">
                Not your manager and not the Exec team. A support admin can open it if you
                ask them to look into a problem — they can already see the answers it was
                written from — and they still cannot publish it. It becomes company-visible
                only if you approve it below.
            </p>
            {/* Version 2 or later means this person has taken the interview more
                than once, so a guide they published from an earlier version may
                still be the one colleagues are reading. `GET /guides/me` returns
                only the NEWEST row, so this cannot be checked — hence a
                conditional possibility rather than a claim. */}
            {(guide.current_profile_version ?? 0) > 1 && (
                <p className="mt-2 text-xs leading-relaxed text-gray-600">
                    If you published a guide from an earlier version of your profile,
                    colleagues can still read that older one. Approving this draft replaces
                    it.
                </p>
            )}
        </div>
    );
};

/** The subject retook the interview, so the guide cites a profile they no longer
 *  have. Distinct from "out of date" hand-waving: it names both versions.
 *
 *  Regenerating after a retake lands on a NEW row (the cache key includes the
 *  profile version), so it never overwrites approved wording and the published
 *  guide keeps standing until the new one is approved. That is why this offers
 *  Regenerate directly, published or not. */
export const StaleBanner: React.FC<{
    guide: GuideDocument;
    onRegenerate: () => void;
    busy: boolean;
}> = ({ guide, onRegenerate, busy }) => (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
        <p className="flex items-center gap-2 text-sm font-medium text-amber-900">
            <AlertTriangle size={16} />
            You have taken the interview again since this was written.
        </p>
        <p className="mt-1 text-xs leading-relaxed text-amber-800">
            This guide was written from profile version {guide.profile_version ?? '—'}; your
            profile is now version {guide.current_profile_version ?? '—'}. Nothing here is
            wrong — it is simply the older answers.
            {guide.published
                ? ' It stays published, and colleagues keep reading this wording, until you approve a new one.'
                : ''}
        </p>
        <div className="mt-3">
            <PrimaryButton onClick={onRegenerate} busy={busy}>
                <RefreshCw size={15} />
                Write it again from my latest answers
            </PrimaryButton>
        </div>
        <p className="mt-2 text-xs text-amber-800">
            That creates a separate draft for the new version. This one is left exactly as
            it is until you approve the new draft.
        </p>
    </div>
);
