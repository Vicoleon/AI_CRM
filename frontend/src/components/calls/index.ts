// Sales Call Capture (CCS) components — the calendar screen and the wrap-up.
//
// Barrel so the two pages have one import line each. Kept as a `.ts` re-export
// file on purpose: a `.tsx` that exports both components and non-components
// trips `react-refresh/only-export-components`.
export { ProposalRow } from './ProposalRow';
export type { ProposalRowProps } from './ProposalRow';
export { EvidenceQuote } from './EvidenceQuote';
export type { EvidenceQuoteProps } from './EvidenceQuote';

// The calendar entry point (Phase 2): today's client calls, bound to HubSpot.
export { MeetingRow } from './MeetingRow';
export type { CapturedCall, MeetingRowProps } from './MeetingRow';
export { ArmingBadge } from './ArmingBadge';
export type { ArmingBadgeProps } from './ArmingBadge';
export { CompanyPicker } from './CompanyPicker';
export type { CompanyPickerProps } from './CompanyPicker';
export { ProductPicker } from './ProductPicker';
export type { ProductPickerProps } from './ProductPicker';
export { PreCallPanel } from './PreCallPanel';
export type { PreCallPanelProps } from './PreCallPanel';
export { AdHocCallForm } from './AdHocCallForm';
export type { AdHocCallFormProps } from './AdHocCallForm';
export { armingCopy, companyPrompt, isImminent, startsInLabel } from './verdictCopy';
export type { ArmingCopy, CompanyPrompt, VerdictTone } from './verdictCopy';
export { DraftProposalCta } from './DraftProposalCta';
export type { DraftProposalCtaProps } from './DraftProposalCta';
