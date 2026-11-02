// Voice commands API client (/api/voice/commands). Mirrors
// backend/routers/voice_commands.py: one command in (a recording, or typed
// text), what to do out. The server never changes anything; an EOS change comes
// back as a card whose `request` the browser sends once the person says yes.
import client from './client';

/** A page the person can open — a row of their own sidebar. */
export interface VoiceDestination {
    path: string;
    label: string;
    group?: string;
}

/** One specific record's page: an X-Ray, an L10, a proposal … */
export interface VoicePlace {
    path: string;
    label: string;
    detail?: string;
    /** Navigation state the page reads (the X-Ray takes the company's name). */
    state?: Record<string, unknown> | null;
}

/** Where an opened record may be (backend voice_records.PLACE_PREFIXES and
 *  PLACE_EXACT). */
const PLACE_PREFIXES = [
    '/customers/', '/eos/meetings/', '/meeting-prep/', '/sales/proposals/',
    '/chat?session=', '/smart-teams/guide/',
];
const PLACE_EXACT = ['/smart-teams'];

export function placeAllowed(path: string): boolean {
    return PLACE_EXACT.includes(path) || PLACE_PREFIXES.some(prefix => path.startsWith(prefix));
}

export interface VoicePlan {
    action: string;
    /** The card's heading: "Add a Rock". */
    title: string;
    /** The confirm button: "Add Rock". */
    confirm: string;
    fields: { label: string; value: string }[];
    /** Exactly what the page's own button sends. */
    request: { method: 'POST' | 'PATCH' | 'PUT'; path: string; body: Record<string, unknown> };
    /** Shown once it worked: "Rock added". */
    done: string;
    open_path?: string | null;
    open_label?: string | null;
    /** How this plan is named in a "which one?" list. */
    option?: string | null;
}

export interface VoiceCommandResult {
    kind: 'navigate' | 'open' | 'ask' | 'action' | 'message' | 'none' | 'answer';
    heard: string;
    destination?: VoiceDestination | null;
    /** kind 'open': where the named record is; several → "which one?". */
    places?: VoicePlace[];
    /** kind 'open': ask even for one place (opening an L10 starts the meeting). */
    ask_first?: boolean;
    /** For 'ask' (and a 'message' that could still go to Ask Cubie). */
    ask?: string | null;
    plan?: VoicePlan | null;
    options?: VoicePlan[];
    prompt?: string | null;
    message?: string | null;
    answer?: 'yes' | 'no' | 'unclear' | null;
}

interface CommandInput {
    clip?: Blob;
    text?: string;
    destinations?: VoiceDestination[];
    page?: string;
    /** 'confirm': the clip is a yes/no answer to the card named in `question`. */
    expect?: 'command' | 'confirm';
    question?: string;
    signal?: AbortSignal;
}

export async function runVoiceCommand({
    clip, text, destinations = [], page = '', expect = 'command', question = '', signal,
}: CommandInput): Promise<VoiceCommandResult> {
    const form = new FormData();
    if (clip) form.append('audio', clip, 'command.wav');
    if (text) form.append('text', text);
    form.append('destinations', JSON.stringify(destinations));
    form.append('page', page);
    form.append('expect', expect);
    if (question) form.append('question', question);
    const { data } = await client.post<VoiceCommandResult>('/api/voice/commands', form, {
        signal,
        // A command reads in a few seconds; this only bounds a hang.
        timeout: 45_000,
    });
    return data;
}

/** Send a confirmed card's request — the same call the page's own button makes,
 *  as the person. Only in-app API paths are ever sent. */
export async function sendVoicePlan(plan: VoicePlan): Promise<void> {
    const { method, path, body } = plan.request;
    if (!path.startsWith('/api/') || path.startsWith('//')) {
        throw new Error('Cubie refused to send that change.');
    }
    await client.request({ method, url: path, data: body });
}

/** Tell the page on screen that a voice command changed something it shows,
 *  so it can reload (the EOS pages listen for their own path). */
export const VOICE_CHANGED_EVENT = 'cubie:voice-changed';

export function announceVoiceChange(path: string | null | undefined): void {
    if (!path) return;
    window.dispatchEvent(new CustomEvent(VOICE_CHANGED_EVENT, { detail: { path } }));
}
