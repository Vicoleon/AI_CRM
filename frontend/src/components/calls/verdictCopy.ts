// Copy for the two verdicts the calendar screen renders: whether transcription
// is armed, and which HubSpot company the meeting is with.
//
// A `.ts` and not a `.tsx` on purpose — `react-refresh/only-export-components`
// trips on a module that exports both components and plain functions, and every
// function here is plain.
//
// THE RULE THIS FILE EXISTS TO ENFORCE: the project's worst outcome is a silent
// no-op, so nothing here may return "fine" by default. Every state has a
// sentence, and the two pairs that look alike stay apart:
//
//   * "no HubSpot company for this domain" vs "we could not ask HubSpot".
//     The first is a fact about the CRM; the second is a lookup failure and the
//     company may well exist. Collapsing them tells an AE to go create a
//     company that is already there.
//   * "not armed yet" vs "will never be armed". A row that cannot be armed
//     (client-hosted space, conference already running, someone else's meeting)
//     is STILL worth capturing — the company binding is what lets the wrap-up
//     write to the right CRM record. So an unarmable row is amber and explained,
//     never red and never silent, and never rendered as armed.
//
// `state` and `reason` are bare strings on the wire because both vocabularies
// are closed SERVER-side. Every lookup below therefore falls back by `state`,
// and then by nothing at all, so a value from a newer backend degrades to a
// neutral badge instead of an empty one.
import {
    externalAttendees,
    type ArmingVerdict,
    type ScheduledMeeting,
} from '../../api/calls';

/** Badge palettes. Full literal Tailwind strings live in the components; this
 *  is only the choice of which one. */
export type VerdictTone = 'green' | 'amber' | 'blue' | 'gray';

export interface ArmingCopy {
    tone: VerdictTone;
    /** Six words at most — it sits on the row beside the time. */
    label: string;
}

/** Per-reason badge copy. Keyed on `reason`, not `state`: the state says
 *  whether we may hope for a transcript, the reason is the only part the AE can
 *  act on ("ask the organizer" is a different action from "join and log it"). */
const ARMING_BY_REASON: Record<string, ArmingCopy> = {
    armed_by_cubie: { tone: 'green', label: 'Transcription on' },
    already_on: { tone: 'green', label: 'Transcription already on' },
    external_host: { tone: 'amber', label: 'Client-hosted — no transcript' },
    not_owner: { tone: 'amber', label: 'Not your meeting — no transcript' },
    already_started: { tone: 'amber', label: 'Already started — cannot arm' },
    arming_unsupported: { tone: 'amber', label: 'Transcription unavailable' },
    write_disabled: { tone: 'amber', label: 'Arming switched off' },
    lookup_failed: { tone: 'amber', label: 'Meet space not found' },
    google_error: { tone: 'amber', label: "Couldn't turn transcription on" },
    // `unknown` state, and deliberately NOT amber: a series that shares one
    // Meet space is a warning about the OTHER occurrences, not a problem with
    // this one. It informs; it does not block.
    recurring_series: { tone: 'blue', label: 'Recurring series' },
};

/** Fallback by state, for a reason this build has never heard of. */
const ARMING_BY_STATE: Record<string, ArmingCopy> = {
    armed: { tone: 'green', label: 'Transcription on' },
    not_armable: { tone: 'amber', label: 'Cannot arm transcription' },
    unknown: { tone: 'gray', label: 'Not checked yet' },
};

const ARMING_UNREADABLE: ArmingCopy = { tone: 'gray', label: 'Transcription unclear' };

/** Badge for one arming verdict.
 *
 *  `hasSession` is what disambiguates the single overloaded reason. On a row
 *  with NO session, `unknown` / `not_checked_yet` means "nobody has asked
 *  Google yet", which is the honest default because `GET /scheduled` performs
 *  no Meet writes at all. On a row that HAS one it means the opposite and is
 *  final: arming is attempted exactly once, at Start, and the only path that
 *  stores this verdict is the confidential one (`_suppressed_arming`), where
 *  Cubie deliberately never asked. "Not checked yet" there would imply
 *  something may still happen. */
export function armingCopy(
    verdict: ArmingVerdict | null | undefined,
    { hasSession }: { hasSession: boolean },
): ArmingCopy {
    const state = verdict?.state ?? '';
    const reason = verdict?.reason ?? '';
    if (hasSession && state === 'unknown' && reason === 'not_checked_yet') {
        return { tone: 'gray', label: 'Not transcribed' };
    }
    return ARMING_BY_REASON[reason] ?? ARMING_BY_STATE[state] ?? ARMING_UNREADABLE;
}

/** What the AE has to do about the company, or null when there is nothing to
 *  do. Null ONLY for `unique` — every other state needs a human, and saying so
 *  is the whole point of the screen. */
export interface CompanyPrompt {
    /** The one-line ask. Names the domain that was tried wherever naming it is
     *  the actionable part. */
    headline: string;
    /** The sentence under it: why this row needs a person. */
    hint: string;
    tone: VerdictTone;
}

/** The first external guest's address, for the personal-address sentence. A
 *  blocked domain suppresses the MATCH, never the ROW — a client on gmail.com
 *  is a real client call, and the row must say "pick the company", never
 *  "internal" and never "no external guest". */
function firstExternalEmail(m: ScheduledMeeting): string | null {
    const external = externalAttendees(m);
    return external.length ? external[0].email : null;
}

export function companyPrompt(m: ScheduledMeeting): CompanyPrompt | null {
    const company = m.company;
    const domain = company?.domain?.trim() || null;
    const count = company?.candidates?.length ?? 0;

    switch (company?.match) {
        case 'unique':
            // The one state with a company id on it. Nothing to ask.
            return null;

        case 'ambiguous':
            // `attendees_truncated` forces this state even on a clean single
            // match, so it gets its own sentence: the reason to look twice is
            // not that HubSpot was unsure, it is that Google hid guests.
            if (m.attendees_truncated) {
                return {
                    tone: 'amber',
                    headline: 'Google truncated this invite — confirm the company.',
                    hint:
                        'The guest list came back shortened, so the external attendee Cubie ' +
                        'can see may not be the only one there was. Pick the company this ' +
                        'call is really with before capturing it.',
                };
            }
            return {
                tone: 'amber',
                headline:
                    count > 1
                        ? `${count} HubSpot companies match${domain ? ` ${domain}` : ''} — pick one.`
                        : 'More than one company could match — pick one.',
                hint:
                    'Cubie will not choose for you: the company it binds is the record the ' +
                    'wrap-up writes to, and picking by list order is how a call note lands ' +
                    'on the wrong client.',
            };

        case 'not_found':
            return {
                tone: 'amber',
                headline: domain
                    ? `${domain} — no HubSpot company. Pick one.`
                    : 'No HubSpot company for this guest. Pick one.',
                hint:
                    'HubSpot answered, and it has no company on that domain. Search for the ' +
                    'right record — or create it in HubSpot first if this is a brand-new ' +
                    'prospect.',
            };

        case 'skipped': {
            const email = firstExternalEmail(m);
            return {
                tone: 'amber',
                headline: email
                    ? `${email} is a personal address — pick the company.`
                    : 'No company domain to look up — pick the company.',
                hint:
                    'A personal or shared mailbox says nothing about which client this is, ' +
                    'so Cubie looked nothing up rather than guess. This is still a client ' +
                    'call — bind it by hand and everything else works normally.',
            };
        }

        case 'error':
            return {
                tone: 'amber',
                headline: 'Cubie could not check HubSpot for this company.',
                hint:
                    (domain ? `${domain} may well be in HubSpot — ` : '') +
                    'the CRM lookup itself failed, which is not the same as "no such ' +
                    'company". Pick the company by hand, or reload in a moment.',
            };

        default:
            // A match state from a newer backend. Ask rather than assume.
            return {
                tone: 'gray',
                headline: 'Confirm the company for this call.',
                hint:
                    'Cubie could not read its own company verdict for this meeting, so ' +
                    'nothing is pre-selected. Pick the company and capture as normal.',
            };
    }
}

/** The countdown line: "in 12 minutes", "started 4 minutes ago", "live now".
 *  Reads the SERVER's signed `starts_in_minutes` and `is_live` — never
 *  `Date.now()`, and never a second opinion derived from `start`. */
export function startsInLabel(m: ScheduledMeeting): string | null {
    if (m.is_live) return 'Live now';
    const mins = m.starts_in_minutes;
    if (mins === null || mins === undefined) return null;
    if (mins < 0) {
        const ago = Math.abs(mins);
        if (ago === 0) return 'Just ended';
        return ago === 1 ? 'Started 1 minute ago' : `Started ${ago} minutes ago`;
    }
    if (mins === 0) return 'Starting now';
    if (mins === 1) return 'In 1 minute';
    if (mins < 60) return `In ${mins} minutes`;
    const hours = Math.floor(mins / 60);
    return hours === 1 ? 'In about an hour' : `In about ${hours} hours`;
}

/** True when the row is close enough that the countdown is worth emphasising. */
export const isImminent = (m: ScheduledMeeting): boolean =>
    m.is_live ||
    (m.starts_in_minutes !== null &&
        m.starts_in_minutes !== undefined &&
        m.starts_in_minutes >= 0 &&
        m.starts_in_minutes <= 15);
