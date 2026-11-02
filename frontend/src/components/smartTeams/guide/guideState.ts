// Pure helpers for the guide surfaces. No JSX, on purpose: the repo's lint rules
// require a module to export either components or plain values, not both
// (`react-refresh/only-export-components`), which is the same split
// `components/smartTeams/interviewFlow.ts` already makes for the interview.
//
// Everything here is a READ of what the server decided. Nothing derives a state
// the API already reports — in particular `stale` is `guide.is_stale`, computed
// server-side from an indexed profile-version key, and is never recomputed by
// comparing the two version numbers on the document.
import { parseInstant } from '../../../utils/datetime.ts';
import type { GuideDocument } from '../../../api/smartTeams';

/** Mirrors `guide.SHORTFALL_LABEL`, which `guide.render_markdown` emits as an H2
 *  when the interview did not support a single guideline.
 *
 *  Matching on somebody else's heading string is a weak hook, so it decides only
 *  whether an EXTRA framing note appears above the document. If the label ever
 *  changes, the note quietly stops appearing and the document still reads
 *  correctly — the failure mode is a missing hint, never a wrong claim. */
const SHORTFALL_HEADING = '## not enough to write from yet';

/** Does this guide say, in the server's own words, that there was not enough to
 *  write from? */
export function isShortfallGuide(markdown: string | null | undefined): boolean {
    if (!markdown) return false;
    return markdown.split('\n').some(line => line.trim().toLowerCase() === SHORTFALL_HEADING);
}

/** A date the way the rest of the app writes one, or nothing at all. */
export function shortDate(iso: string | null | undefined): string | null {
    const date = parseInstant(iso);
    if (!date) return null;
    return date.toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
    });
}

/** What state the subject's own guide is in, as one word the page can switch on.
 *
 *  `retracted` is a real state and not "draft again": `approved_at` survives an
 *  unpublish on purpose (it is the record that consent was once given), so the
 *  page can say "you published this and took it back" instead of pretending it
 *  was never out there. */
export type OwnGuidePhase = 'published' | 'retracted' | 'draft';

export function ownGuidePhase(guide: GuideDocument): OwnGuidePhase {
    if (guide.published) return 'published';
    if (guide.approved_at) return 'retracted';
    return 'draft';
}
