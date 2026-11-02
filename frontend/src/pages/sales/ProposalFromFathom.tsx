/** `/sales/proposals/from-fathom/:recordingId` — the Sales Coach's hand-off.
 *
 *  The Coach knows every Fathom recording by id and shows "Continue in Cubie"
 *  on its Call insights card. This page is where that link lands: it asks the
 *  server for the proposal behind the recording (attaching the recording as a
 *  captured call first if the Fathom webhook has not yet), then forwards to the
 *  proposal screen. It renders only while that happens, and it renders each
 *  refusal as its own sentence rather than a blank page:
 *
 *  - 404: Cubie has not received this recording (and Fathom's recent list does
 *         not have it either) — usually "give it a minute".
 *  - 403: another AE's call, or the caller's team is not onboarded.
 *  - 409: Fathom has it but it is not a client call (internal-only, no
 *         transcript, confidential, too old) — the server's sentence says which.
 */
import React, { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { FileSignature } from 'lucide-react';
import { isNotEnabled, proposalsError, startProposalFromFathom } from '../../api/proposals';
import { BackButton } from '../../components/BackButton';
import { EosPageHeader, ErrorState, LoadingState, PageContainer } from '../eos/_shared';

export const ProposalFromFathom: React.FC = () => {
    const { recordingId } = useParams<{ recordingId: string }>();
    const navigate = useNavigate();
    const [error, setError] = useState<string | null>(null);

    // Derived, not set in the effect: a missing param is a fact about the URL,
    // and react-hooks/set-state-in-effect flags a synchronous setState there.
    const missing = !recordingId;

    useEffect(() => {
        if (!recordingId) return;
        let cancelled = false;
        startProposalFromFathom(recordingId)
            .then(out => {
                if (!cancelled) navigate(`/sales/proposals/${encodeURIComponent(out.proposal.id)}`, { replace: true });
            })
            .catch(e => {
                if (cancelled) return;
                const err = proposalsError(e, 'Cubie could not open this recording.');
                setError(
                    isNotEnabled(err)
                        ? 'Proposal drafting is not switched on in this environment.'
                        : err.status === 403
                          ? 'This recording is another AE’s call, or proposal drafting is not open to your team yet.'
                          : err.message,
                );
            });
        return () => {
            cancelled = true;
        };
    }, [recordingId, navigate]);

    return (
        <PageContainer>
            <EosPageHeader
                icon={<FileSignature size={20} />}
                title="Proposal"
                subtitle="Opening the call behind this Fathom recording"
                right={<BackButton fallback="/calls/start" label="Today's Calls" />}
            />
            {missing || error ? (
                <ErrorState message={error ?? 'No Fathom recording was named in the link.'} />
            ) : (
                <LoadingState label="Reading the call… this takes about half a minute the first time." />
            )}
        </PageContainer>
    );
};

export default ProposalFromFathom;
