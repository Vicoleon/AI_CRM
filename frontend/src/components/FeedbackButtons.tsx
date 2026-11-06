// Inline WGLL feedback control — 👍 useful / 😐 meh / 👎 not useful on a single
// Cubie-generated proposal (AI Priorities) or message (Chat). One click sets the
// rating; clicking a different face changes it. After a rating, a small optional
// "Why?" note appears (never required). Captures a text snapshot so the feedback
// stays meaningful even if the source later changes.
//
// Reused on both surfaces — pass `surface`, the stable `proposalId`, and the
// `proposalText` snapshot. Persists to POST /api/feedback (upsert).
import React, { useEffect, useRef, useState } from 'react';
import { ThumbsUp, ThumbsDown, Meh, Check } from 'lucide-react';
import client from '../api/client';

export type FeedbackRating = 'useful' | 'meh' | 'not_useful';
export type FeedbackSurface = 'priorities' | 'chat' | 'meeting_prep' | 'xray' | 'crm_sync';

interface FeedbackButtonsProps {
    surface: FeedbackSurface;
    proposalId: string;
    proposalText: string;
    proposalMetadata?: Record<string, any>;
    initialRating?: FeedbackRating | null;
    initialComment?: string | null;
    className?: string;
    // Hide the inline "Helpful?" mini-label — use when the surrounding surface
    // supplies its own heading (e.g. the Meeting Prep "Was this briefing helpful?"
    // footer) so the prompt isn't stated twice.
    hideLabel?: boolean;
}

const FACES: { value: FeedbackRating; label: string; Icon: typeof ThumbsUp; on: string }[] = [
    { value: 'useful', label: 'Useful', Icon: ThumbsUp, on: 'bg-green-100 text-green-700 ring-1 ring-green-300' },
    { value: 'meh', label: 'Meh', Icon: Meh, on: 'bg-amber-100 text-amber-700 ring-1 ring-amber-300' },
    { value: 'not_useful', label: 'Not useful', Icon: ThumbsDown, on: 'bg-red-100 text-red-700 ring-1 ring-red-300' },
];

export const FeedbackButtons: React.FC<FeedbackButtonsProps> = ({
    surface,
    proposalId,
    proposalText,
    proposalMetadata,
    initialRating = null,
    initialComment = null,
    className = '',
    hideLabel = false,
}) => {
    const [rating, setRating] = useState<FeedbackRating | null>(initialRating);
    const [comment, setComment] = useState<string>(initialComment || '');
    const [savedComment, setSavedComment] = useState<string>(initialComment || '');
    const [showComment, setShowComment] = useState<boolean>(!!initialRating);
    const [busy, setBusy] = useState(false);
    const [justSaved, setJustSaved] = useState(false);

    // Re-hydrate from props once the parent's restored rating arrives. Parents
    // load the caller's existing rating from GET /api/feedback/mine in a
    // separate async effect that resolves AFTER this component has already
    // mounted — but useState's initializer only runs on the first render, so
    // without this the restored face never reaches state and the rating looks
    // "lost" when the user navigates away and comes back. We keep adopting the
    // incoming props until the user interacts here, after which the local
    // (optimistic) state is authoritative and must not be clobbered.
    const hasInteracted = useRef(false);
    useEffect(() => {
        // A different item mounted into this slot → accept its restored state.
        hasInteracted.current = false;
    }, [proposalId]);
    useEffect(() => {
        if (hasInteracted.current) return;
        setRating(initialRating);
        setComment(initialComment || '');
        setSavedComment(initialComment || '');
        setShowComment(!!initialRating);
    }, [proposalId, initialRating, initialComment]);

    const persist = async (nextRating: FeedbackRating, nextComment: string) => {
        setBusy(true);
        try {
            await client.post('/api/feedback', {
                surface,
                proposal_id: proposalId,
                proposal_text: proposalText,
                proposal_metadata: proposalMetadata,
                rating: nextRating,
                comment: nextComment.trim() ? nextComment.trim() : null,
            });
            setSavedComment(nextComment);
            setJustSaved(true);
            window.setTimeout(() => setJustSaved(false), 1500);
        } catch (e) {
            console.error('Failed to save feedback', e);
        } finally {
            setBusy(false);
        }
    };

    const handleRate = (value: FeedbackRating) => {
        if (busy) return;
        hasInteracted.current = true;  // local choice now wins over prop hydration
        const prev = rating;
        setRating(value);          // optimistic
        setShowComment(true);
        if (value !== prev) {
            persist(value, comment);
        }
    };

    const handleCommentBlur = () => {
        if (!rating) return;
        if (comment.trim() === savedComment.trim()) return;  // no change
        persist(rating, comment);
    };

    return (
        <div className={`flex flex-col gap-1.5 ${className}`}>
            <div className="flex items-center gap-1.5">
                {!hideLabel && (
                    <span className="text-[10px] uppercase tracking-tight text-gray-400 mr-0.5 select-none">
                        Helpful?
                    </span>
                )}
                {FACES.map(({ value, label, Icon, on }) => {
                    const selected = rating === value;
                    return (
                        <button
                            key={value}
                            type="button"
                            aria-pressed={selected}
                            aria-label={label}
                            title={label}
                            disabled={busy}
                            onClick={(e) => { e.stopPropagation(); handleRate(value); }}
                            className={`p-1 rounded transition-colors disabled:opacity-50 ${
                                selected ? on : 'text-gray-400 hover:text-gray-600 hover:bg-gray-100'
                            }`}
                        >
                            <Icon className="h-3.5 w-3.5" />
                        </button>
                    );
                })}
                {justSaved && (
                    <span className="inline-flex items-center gap-0.5 text-[10px] text-green-600">
                        <Check className="h-3 w-3" /> Saved
                    </span>
                )}
            </div>

            {showComment && (
                <input
                    type="text"
                    value={comment}
                    onChange={(e) => { hasInteracted.current = true; setComment(e.target.value); }}
                    onClick={(e) => e.stopPropagation()}
                    onBlur={handleCommentBlur}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter') { e.preventDefault(); (e.target as HTMLInputElement).blur(); }
                    }}
                    placeholder="Why? (optional)"
                    className="w-full max-w-md text-xs border border-gray-200 rounded px-2 py-1 text-gray-700 placeholder:text-gray-400 focus:outline-none focus:ring-1 focus:ring-brand/40"
                />
            )}
        </div>
    );
};

export default FeedbackButtons;
