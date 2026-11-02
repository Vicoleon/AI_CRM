// The only file that talks to the Google Meet Add-ons SDK.
//
// All the panel needs from Meet is the meeting code of the call it is open in
// (`getMeetingInfo().meetingCode`, "aaa-bbbb-ccc"); the backend turns that into
// the AE's call and its checklist. Three things here are deliberate:
//
// 1. THE SESSION IS CREATED ONCE PER PAGE, at module scope, never in a React
//    effect. The SDK refuses a second `createAddonSession` with
//    `AddonSessionAlreadyCreated`, and StrictMode runs every effect twice in
//    development — an effect-owned session would fail on every dev load.
// 2. "AM I INSIDE MEET?" IS ANSWERED BY THE URL, not by trying. Meet appends
//    `meet_sdk` (and friends) to the iframe URL it loads; without it the SDK can
//    only fail, slowly. A page opened directly — a test link, a developer — is
//    STANDALONE and may name a code with `?meeting_code=`. That exposes nothing:
//    the backend answers only for the signed-in AE's own calendar.
// 3. THE PANEL NEVER STARTS AN ACTIVITY. `startActivity` would invite every
//    participant — the client included — to open the add-on. The checklist is
//    for the AE alone, so this file does not expose it.
import { meet } from '@googleworkspace/meet-addons/meet.addons';

export type MeetConnection =
    | { kind: 'meet'; meetingCode: string; meetingId: string }
    | { kind: 'standalone'; meetingCode: string | null }
    | { kind: 'failed'; message: string; detail: string | null };

/** Meet adds this to the side panel's URL; the SDK reads it the same way. */
export const isInsideMeet = (search: string = window.location.search): boolean =>
    new URLSearchParams(search).has('meet_sdk');

const CONNECT_TIMEOUT_MS = 15_000;

let pending: Promise<MeetConnection> | null = null;

/** The panel's connection to Meet, made once and shared (note 1). */
export function connectToMeet(): Promise<MeetConnection> {
    if (!pending) pending = connect();
    return pending;
}

function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        const timer = window.setTimeout(
            () => reject(new Error('Meet did not answer the add-on in time.')),
            ms,
        );
        work.then(
            value => {
                window.clearTimeout(timer);
                resolve(value);
            },
            error => {
                window.clearTimeout(timer);
                reject(error);
            },
        );
    });
}

function describe(error: unknown): string | null {
    if (error && typeof error === 'object') {
        const typed = error as { errorType?: unknown; message?: unknown };
        if (typeof typed.errorType === 'string') return typed.errorType;
        if (typeof typed.message === 'string') return typed.message;
    }
    return error ? String(error) : null;
}

async function connect(): Promise<MeetConnection> {
    if (!isInsideMeet()) {
        const code = new URLSearchParams(window.location.search).get('meeting_code');
        return { kind: 'standalone', meetingCode: code && code.trim() ? code.trim() : null };
    }
    const projectNumber = String(import.meta.env.VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER || '').trim();
    if (!projectNumber) {
        return {
            kind: 'failed',
            message: 'This build of Cubie does not know which Google Cloud project the add-on belongs to.',
            detail: 'VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER was empty at build time.',
        };
    }
    try {
        const work = (async () => {
            const session = await meet.addon.createAddonSession({ cloudProjectNumber: projectNumber });
            const client = await session.createSidePanelClient();
            return client.getMeetingInfo();
        })();
        const info = await withTimeout(work, CONNECT_TIMEOUT_MS);
        const code = (info?.meetingCode || '').trim();
        if (!code) {
            return {
                kind: 'failed',
                message: 'Meet did not say which call this is.',
                detail: 'getMeetingInfo() returned no meetingCode.',
            };
        }
        return { kind: 'meet', meetingCode: code, meetingId: info.meetingId || '' };
    } catch (error) {
        return {
            kind: 'failed',
            message: 'Cubie could not connect to this Meet call.',
            detail: describe(error),
        };
    }
}
