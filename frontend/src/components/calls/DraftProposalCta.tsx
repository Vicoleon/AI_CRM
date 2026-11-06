/** "Draft proposal" — the wrap-up's hand-off to the Proposal Generator.
 *
 *  One button with two states. If a proposal already exists for this call it
 *  reads "Open proposal" and navigates; otherwise it starts one (the server
 *  reads the transcript and fills the brief, which takes tens of seconds) and
 *  then navigates. The server is idempotent per call, so a double-click or a
 *  second tab lands on the same proposal either way.
 *
 *  Hidden when the caller cannot reach the feature (`canAccessProposals`
 *  false), when the server says the feature is off here (404), or when the
 *  caller is not the AE on the call (403): a manager reading their team's
 *  wrap-up gets no button rather than a button that fails.
 */
import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FileSignature } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import {
    fetchProposalForCall,
    proposalsError,
    startProposalFromCall,
} from '../../api/proposals';
import { toast } from '../eos/Toast';
import { PrimaryButton, SecondaryButton } from '../smartTeams/shared';

export interface DraftProposalCtaProps {
    sessionId: string;
    /** Primary styling — used in the "the prospect asked for one" band. */
    emphasized?: boolean;
}

export const DraftProposalCta: React.FC<DraftProposalCtaProps> = ({ sessionId, emphasized }) => {
    const { canAccessProposals } = useAuth();
    const navigate = useNavigate();
    // undefined = not yet asked; null = none exists; string = its id.
    const [existing, setExisting] = useState<string | null | undefined>(undefined);
    const [hidden, setHidden] = useState(false);
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (!canAccessProposals) return;
        let cancelled = false;
        fetchProposalForCall(sessionId)
            .then(r => {
                if (!cancelled) setExisting(r.proposal_id);
            })
            .catch(() => {
                // Off here, or not our call. Either way: no button.
                if (!cancelled) setHidden(true);
            });
        return () => {
            cancelled = true;
        };
    }, [sessionId, canAccessProposals]);

    if (!canAccessProposals || hidden || existing === undefined) return null;

    const onClick = async () => {
        if (existing) {
            navigate(`/sales/proposals/${encodeURIComponent(existing)}`);
            return;
        }
        setBusy(true);
        try {
            const proposal = await startProposalFromCall(sessionId);
            navigate(`/sales/proposals/${encodeURIComponent(proposal.id)}`);
        } catch (e) {
            toast.error(proposalsError(e, 'Cubie could not start the proposal.').message);
        } finally {
            setBusy(false);
        }
    };

    const label = existing ? 'Open proposal' : busy ? 'Reading the call…' : 'Draft proposal';
    const Button = emphasized ? PrimaryButton : SecondaryButton;
    return (
        <Button onClick={() => void onClick()} busy={busy}>
            <FileSignature size={15} />
            {label}
        </Button>
    );
};

export default DraftProposalCta;
