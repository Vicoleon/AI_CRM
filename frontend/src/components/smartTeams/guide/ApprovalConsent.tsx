// Approval is consent, and this is the screen that has to behave like it.
//
// WHAT THAT MEANS IN PRACTICE
// ---------------------------
//  * The guide is rendered ABOVE this panel, in full, unedited. "Before
//    approving, the person sees exactly what colleagues will see" is satisfied
//    by the document itself being the thing on screen — not by a summary of it,
//    and not by a preview modal a person can approve without opening.
//  * Nothing is pre-checked and nothing defaults towards publishing. The
//    checkbox starts false on every mount, there is no "remember this", and the
//    Approve button is genuinely disabled (and `aria-disabled`) until it is
//    ticked. A person who lands here and does nothing has published nothing.
//  * The consequences are listed before the control, in the person's terms,
//    including the two that are easy to leave out: that Cubie Chat can quote a
//    published guide, and that unpublishing exists and is immediate in the app.
//  * `guide.approve` is the only writer of `published = True` and it refuses
//    anybody but the subject — an admin and an exec included. That is stated
//    here because "somebody could do this for me" is exactly the wrong mental
//    model for a consent switch.
//
// There is deliberately no second confirmation dialog on top of the checkbox.
// Two gates for one decision trains people to click through both.
import React, { useState } from 'react';
import { AlertTriangle, Eye, EyeOff, Globe, Info, Lock, MessageSquare, Undo2 } from 'lucide-react';

import { PrimaryButton, SecondaryButton } from '../shared';

const Bullet: React.FC<{ icon: React.ReactNode; children: React.ReactNode }> = ({
    icon,
    children,
}) => (
    <li className="flex items-start gap-2.5">
        <span className="mt-0.5 flex-shrink-0 text-gray-400">{icon}</span>
        <span className="text-sm leading-relaxed text-gray-700">{children}</span>
    </li>
);

/** A failed mutation, said out loud with the server's own sentence and a way to
 *  try again — the button below is the retry. Never a toast that disappears:
 *  "did my guide publish?" must not be a question the UI leaves open. */
const MutationError: React.FC<{ message: string }> = ({ message }) => (
    <div role="alert" className="mt-3 flex items-start gap-2.5 rounded-md border border-red-200 bg-red-50 p-3">
        <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
        <div>
            <p className="text-sm font-medium text-red-900">Nothing was published.</p>
            <p className="mt-1 text-xs leading-relaxed text-red-800">{message}</p>
        </div>
    </div>
);

export const ApprovalConsent: React.FC<{
    /** True when this exact guide was published before and then retracted. */
    previouslyPublished: boolean;
    busy: boolean;
    error: string | null;
    onApprove: () => void;
}> = ({ previouslyPublished, busy, error, onApprove }) => {
    // Local, unchecked, and reset by any remount. There is no persisted consent
    // and no URL parameter that can arrive pre-ticked.
    const [understood, setUnderstood] = useState(false);

    return (
        <section
            aria-labelledby="st-approve-heading"
            className="rounded-lg border border-gray-200 bg-white p-5"
        >
            <h2
                id="st-approve-heading"
                className="flex items-center gap-2 text-base font-semibold text-gray-900"
            >
                <Globe size={18} className="text-brand" />
                {previouslyPublished
                    ? 'Publish this guide again?'
                    : 'Publishing this guide is your decision'}
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-gray-700">
                What you have just read is exactly what colleagues will see. Cubie does not
                add anything to it at publication and does not hold anything back.
            </p>

            <ul className="mt-3 space-y-2">
                <Bullet icon={<Eye size={15} />}>
                    Anybody at your company with a Cubie account can open it and read it in full.
                </Bullet>
                <Bullet icon={<MessageSquare size={15} />}>
                    Cubie Chat can quote it when a colleague asks how to work with you. That
                    copy is made in the background and takes a few minutes.
                </Bullet>
                <Bullet icon={<Undo2 size={15} />}>
                    You can take it back out of company view whenever you like. Colleagues
                    lose access immediately; the Chat copy is removed a moment later.
                </Bullet>
                <Bullet icon={<Lock size={15} />}>
                    Nobody can do this for you. A support admin, your manager and the Exec
                    team are all refused — publishing is the one thing only you can do here.
                </Bullet>
            </ul>

            <label className="mt-4 flex cursor-pointer items-start gap-2.5 rounded-md border border-gray-200 bg-gray-50 p-3">
                <input
                    type="checkbox"
                    checked={understood}
                    onChange={event => setUnderstood(event.target.checked)}
                    className="mt-0.5 h-4 w-4 flex-shrink-0 rounded border-gray-300 text-brand focus:ring-brand"
                />
                <span className="text-sm leading-relaxed text-gray-800">
                    I have read this guide and I want everyone at my company to be able to read it.
                </span>
            </label>

            {error && <MutationError message={error} />}

            <div className="mt-4 flex flex-wrap items-center gap-3">
                <PrimaryButton onClick={onApprove} disabled={!understood} busy={busy}>
                    <Globe size={15} />
                    Publish to everyone at your company
                </PrimaryButton>
                {!understood && (
                    <span className="text-xs text-gray-500">
                        Tick the box above when you are ready.
                    </span>
                )}
            </div>
            <p className="mt-3 text-xs leading-relaxed text-gray-500">
                Not ready? Leaving this page changes nothing — the draft stays private to you,
                and you can come back to it.
            </p>
        </section>
    );
};

/** Retraction. The safe direction, so it is one button and a plain sentence
 *  rather than a dialog — and the two latencies are named separately, because
 *  "unpublished" alone would hide the one that is still true for a minute. */
export const UnpublishPanel: React.FC<{
    ragIngested: boolean;
    busy: boolean;
    error: string | null;
    onUnpublish: () => void;
}> = ({ ragIngested, busy, error, onUnpublish }) => (
    <section
        aria-labelledby="st-unpublish-heading"
        className="rounded-lg border border-gray-200 bg-white p-5"
    >
        <h2
            id="st-unpublish-heading"
            className="flex items-center gap-2 text-base font-semibold text-gray-900"
        >
            <EyeOff size={18} className="text-gray-500" />
            Take it out of company view
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-gray-700">
            Colleagues stop being able to open it the moment you press this. Nothing is
            deleted — the guide stays here, and publishing it again is a single decision away.
        </p>
        {ragIngested && (
            <p className="mt-2 flex items-start gap-2 text-xs leading-relaxed text-gray-600">
                <Info className="mt-0.5 flex-shrink-0 text-gray-400" size={13} />
                Cubie also has to remove the copy in Chat&apos;s knowledge base. That runs in
                the background, so for a short while Chat may still quote it even though the
                document itself is out of view.
            </p>
        )}
        {error && (
            <div role="alert" className="mt-3 flex items-start gap-2.5 rounded-md border border-red-200 bg-red-50 p-3">
                <AlertTriangle className="mt-0.5 flex-shrink-0 text-red-500" size={16} />
                <div>
                    <p className="text-sm font-medium text-red-900">
                        Your guide is still published.
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-red-800">{error}</p>
                </div>
            </div>
        )}
        <div className="mt-4">
            <SecondaryButton onClick={onUnpublish} busy={busy}>
                <EyeOff size={15} />
                Take it out of company view
            </SecondaryButton>
        </div>
    </section>
);
