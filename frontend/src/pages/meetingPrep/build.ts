// Background builds of a Meeting Prep brief: reading what the server says
// about one.
//
// A full build reads Gmail, Calendar, Drive, the CRM and past transcripts and
// then runs a Pro model call: a minute or two. The page used to wait on it
// with a spinner, so the reader sat there. Now the build runs in the
// background: the server answers at once (202) with a handle for the build,
// the page polls that handle, and the reader may leave. Cubie sends an in-app
// notification with a link when the version is ready (or could not be built).
//
// The build IS the next version: the server writes a pending row first and
// finishes that same row as v<N>, so `run_id` names the version once it is
// ready and `getVersion(run_id)` opens it.
//
// Import-free so it runs under Node's test runner.

export type BuildKind = 'first' | 'regenerate';
export type BuildState = 'queued' | 'running' | 'ready' | 'failed';

/** A build in flight: the 202 answer, `pending_run` on a brief, or
 *  `pending` on the version list. */
export interface PendingBuild {
    run_id: string;
    status: 'queued' | 'running';
    kind: BuildKind;
    /** ISO, naive UTC. */
    started_at: string | null;
    event_id: string | null;
    /** The meeting's title, when the server knows it yet. */
    title: string | null;
}

/** GET /prep-runs/{run_id}/status. */
export interface BuildStatus {
    run_id: string;
    event_id: string | null;
    status: BuildState;
    kind: BuildKind;
    /** The version this build became (ready only). */
    version: number | null;
    /** Why it failed, worded for the reader (failed only). */
    detail: string | null;
    started_at: string | null;
    finished_at: string | null;
}

/** What opening a meeting's brief answers: the latest version, or the build
 *  that is making the first one. */
export type PrepAnswer<T> = { kind: 'ready'; data: T } | { kind: 'building'; build: PendingBuild };

/** How often the page asks how a build is going. */
export const BUILD_POLL_MS = 4_000;
/** The slowest the page backs off to while the status call keeps failing
 *  (a network blip must not end the wait, but must not hammer either). */
export const BUILD_POLL_MAX_MS = 30_000;

export const LEAVE_MESSAGE = "You can leave this page — Cubie will notify you with a link when it's ready.";
export const TYPICAL_DURATION = 'Usually ready in 1–2 minutes.';
export const FIRST_BUILD_FAILED = "Cubie couldn't build this briefing. Please try again.";
export const REGENERATE_FAILED = "Cubie couldn't build a new version. Your current version is unchanged.";
export const BUILD_LOST = "Cubie lost track of this build. Please try again.";

type Loose = Record<string, unknown>;

const asObject = (v: unknown): Loose | null =>
    v && typeof v === 'object' && !Array.isArray(v) ? (v as Loose) : null;
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const kindOf = (v: unknown): BuildKind => (v === 'regenerate' ? 'regenerate' : 'first');

/** A build in flight, or null when `body` is not one (no run id, or a status
 *  that is not queued/running — a settled build is not "pending"). */
export function readPendingBuild(body: unknown): PendingBuild | null {
    const b = asObject(body);
    const runId = str(b?.run_id);
    if (!b || !runId) return null;
    if (b.status !== 'queued' && b.status !== 'running') return null;
    return {
        run_id: runId,
        status: b.status,
        kind: kindOf(b.kind),
        started_at: str(b.started_at),
        event_id: str(b.event_id),
        title: str(b.title),
    };
}

/** The 202 a build-starting call answers with. Lenient where
 *  `readPendingBuild` is strict: a build that finished (or failed) between
 *  being started and being answered still has a handle worth following, so it
 *  reads as running and the first status poll settles it. Null only without a
 *  run id. */
export function readAcceptedBuild(body: unknown): PendingBuild | null {
    const b = asObject(body);
    if (!b || !str(b.run_id)) return null;
    return readPendingBuild({ ...b, status: b.status === 'queued' ? 'queued' : 'running' });
}

/** GET /meeting-prep/{event_id}: 200 is the latest version, 202 a build that
 *  is making the first one. */
export function readPrepAnswer<T>(status: number, body: unknown): PrepAnswer<T> {
    if (status === 202) {
        const build = readAcceptedBuild(body);
        if (!build) throw new Error(FIRST_BUILD_FAILED);
        return { kind: 'building', build };
    }
    return { kind: 'ready', data: body as T };
}

/** The status answer, normalised. A status the page does not know reads as
 *  still running: the server settles every build (a stuck one is reported
 *  failed after its time limit), so waiting is always safe. */
export function readBuildStatus(body: unknown, runId: string): BuildStatus {
    const b = asObject(body) ?? {};
    const raw = b.status;
    const status: BuildState =
        raw === 'queued' || raw === 'running' || raw === 'ready' || raw === 'failed' ? raw : 'running';
    return {
        run_id: str(b.run_id) ?? runId,
        event_id: str(b.event_id),
        status,
        kind: kindOf(b.kind),
        version: num(b.version),
        detail: str(b.detail),
        started_at: str(b.started_at),
        finished_at: str(b.finished_at),
    };
}

export const isSettled = (s: { status: BuildState }): boolean => s.status === 'ready' || s.status === 'failed';

/**
 * A brief came back (200) for `eventId`: the builds of this meeting the page
 * was following when it asked that the server no longer reports in flight.
 * They settled while nobody was watching (the reader went to another meeting
 * and back), so the page drops them quietly — no "Building" banner that is no
 * longer true, no second "ready" toast, and no switching to the version they
 * made when the brief just opened is that version or newer.
 *
 * `followed` is the page's list as it was when the request was SENT: a build
 * started after that (a Regenerate clicked while it was in flight) is not one
 * this answer can know about, so it is never dropped.
 */
export function staleBuilds(
    followed: Iterable<readonly [runId: string, eventId: string]>,
    eventId: string,
    serverPending: { run_id: string } | null | undefined,
): string[] {
    const stale: string[] = [];
    for (const [runId, forEvent] of followed) {
        if (forEvent === eventId && runId !== serverPending?.run_id) stale.push(runId);
    }
    return stale;
}

/**
 * A version landed (`built`) while another is on screen. `show` it, `offer`
 * it (the banner — when the reader is holding the screen: an unsaved edit, a
 * write in flight, an older version open, or its number is unknown), or say
 * nothing (`quiet`) when it is not newer than what is already on screen — the
 * page never switches anyone back to an older version.
 */
export type ReadyAction = 'show' | 'offer' | 'quiet';

export function readyAction(built: number | null, onScreen: number | null, holding: boolean): ReadyAction {
    if (built !== null && onScreen !== null && built <= onScreen) return 'quiet';
    return holding || built === null ? 'offer' : 'show';
}

/** The wait before the next status call: steady while it answers, backing
 *  off while it fails. */
export function nextPollDelay(consecutiveFailures: number): number {
    if (consecutiveFailures <= 0) return BUILD_POLL_MS;
    return Math.min(BUILD_POLL_MS * 2 ** consecutiveFailures, BUILD_POLL_MAX_MS);
}

/** "0:07", "1:42", "1:02:03". */
export function formatElapsed(ms: number | null | undefined): string {
    const total = Math.max(0, Math.floor((ms ?? 0) / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const ss = String(s).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** How full the waiting bar is, 0..0.95. Time-based, not real progress (the
 *  server reports no steps): it eases toward full over the usual minute or
 *  two and never reaches it, so it cannot claim "done" before the build is. */
export function waitingBarFraction(elapsedMs: number | null | undefined): number {
    const ms = Math.max(0, elapsedMs ?? 0);
    return 0.95 * (1 - Math.exp(-ms / 60_000));
}

/** The wording for a build's state, for the status pill. */
export const buildStateLabel = (status: BuildState): string =>
    status === 'queued' ? 'Queued' : status === 'running' ? 'Building' : status === 'ready' ? 'Ready' : 'Failed';
