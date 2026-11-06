// Meeting Prep API client: the brief, its versions, and same-canvas edits.
//
// Uses the shared axios instance (bearer token, request id and impersonation
// headers come from its interceptor); full /api/... paths, no baseURL.
//
// Every change to a brief — a full regenerate, one section rewritten by Cubie,
// one section edited by hand, a restore — comes back as a NEW version (the
// whole brief), and nothing is overwritten. Edits, rewrites and restores are
// only accepted on the latest version; anything else answers 409 with the
// newer version's number (see `readPrepError`).
//
// Full builds (the first version, and every Regenerate) run in the
// BACKGROUND: the call answers at once (202) with the build's handle, the page
// polls `getBuildStatus`, and the reader may leave — Cubie notifies them with a
// link when the version is ready. See `pages/meetingPrep/build.ts`.
//
// Routes: GET /meeting-prep/{event_id:path} is a catch-all, so everything
// version-shaped lives under /prep-runs and takes the event id as a query
// parameter or in the body rather than in the path.
import client from './client';
import type { AttendeeIntel } from '../components/AttendeeIntel';
import {
    readAcceptedBuild,
    readBuildStatus,
    readPendingBuild,
    readPrepAnswer,
    type BuildStatus,
    type PendingBuild,
    type PrepAnswer,
} from '../pages/meetingPrep/build';
import type { SectionKey, SectionMetaEntry, Stakeholder } from '../pages/meetingPrep/sections';
import type { AccountSnapshot, SignalUsed } from '../pages/meetingPrep/snapshot';

export {
    readPrepError,
    isStaleVersion,
    newerVersion,
    writeOutcome,
    STILL_WORKING_MESSAGE,
    UNSAVED_MESSAGE,
    type PrepError,
} from '../pages/meetingPrep/errors';
export type { AccountSnapshot, SignalUsed, SectionKey, SectionMetaEntry, Stakeholder };
export type { BuildStatus, PendingBuild, PrepAnswer };

/** A one-section rewrite is still answered in the request: a Pro call (with
 *  web search on a battle brief) whose retries alone can pass 105s, so it
 *  waits longer than the client's 105s default. A browser that gives up early
 *  does not stop the server, which may still save the version it was writing.
 *  Full builds no longer need it — they answer at once and run in the
 *  background. */
export const GENERATION_TIMEOUT_MS = 180_000;

export type ChangeKind =
    | 'generated'
    | 'regenerated'
    | 'section_rewritten'
    | 'section_edited'
    | 'restored'
    | 'failed';

/** Mirrors `MeetingPrepResponse` in backend/routers/home.py. */
export interface MeetingPrepData {
    title: string;
    executive_summary: string;
    meeting_objective: string;
    client_profile: string;
    /** A JSON-encoded array of {name, email, role, context} (older briefs may
     *  hold a pipe-separated string or markdown). */
    stakeholder_map: string;
    recent_interactions: string;
    past_meeting_context: string;
    product_fit_analysis: string;
    recommended_positioning: string;
    likely_objections: string;
    discovery_questions: string[];
    suggested_agenda: string[];
    talking_points: string[];
    next_steps: string[];
    /** Unanswered email / unkept commitments owed to an attendee. */
    open_items_you_owe?: string[];
    source_evidence: string[];
    partial_errors: string[];
    recommended_products: string[];
    /** UC-47 — researched public record for each EXTERNAL attendee. */
    attendee_intel?: AttendeeIntel[];
    is_sales_user: boolean;
    is_internal: boolean;
    /** The persisted run — the handle for Word, Assets and every version call. */
    run_id?: string | null;

    // Newer signals
    account_snapshot?: AccountSnapshot | null;
    signals_used?: SignalUsed[];
    /** The meeting's start, already formatted in the reader's timezone. */
    meeting_time?: string | null;

    // Version (always hydrated from the row, never from the stored brief)
    version?: number | null;
    latest_version?: number | null;
    is_latest?: boolean;
    base_version?: number | null;
    /** ISO, naive UTC. */
    created_at?: string | null;
    change_kind?: ChangeKind | string | null;
    change_note?: string | null;
    changed_section?: string | null;
    section_meta?: Record<string, SectionMetaEntry>;
    generation_failed?: boolean;
    /** A build for this meeting still in flight (e.g. a Regenerate started in
     *  another tab, or before the reader left). */
    pending_run?: PendingBuild | null;
}

/** One row of a meeting's version list (no brief body). */
export interface VersionSummary {
    run_id: string;
    version: number;
    created_at?: string | null;
    change_kind?: ChangeKind | string | null;
    change_note?: string | null;
    changed_section?: string | null;
    base_version?: number | null;
    is_latest: boolean;
}

export interface VersionList {
    event_id: string;
    latest_version: number | null;
    /** Newest first. */
    versions: VersionSummary[];
    /** A build for this meeting still in flight, if any. */
    pending?: PendingBuild | null;
}

/** What a section's value may be on save. */
export type SectionValue = string | string[] | Stakeholder[];

const runPath = (runId: string) => `/api/homefeed/prep-runs/${encodeURIComponent(runId)}`;

/** The caller's latest version for this meeting (`ready`), or — when there
 *  is none yet — the background build making v1 (`building`; the server starts
 *  one, or hands back the one already in flight). */
export const getPrep = (eventId: string, focusOn?: string): Promise<PrepAnswer<MeetingPrepData>> =>
    client
        .get<unknown>(`/api/homefeed/meeting-prep/${encodeURIComponent(eventId)}`, {
            params: focusOn?.trim() ? { focus_on: focusOn.trim() } : undefined,
        })
        .then(r => readPrepAnswer<MeetingPrepData>(r.status, r.data));

/** The caller's versions of this meeting's brief, newest first (plus the
 *  build in flight, if any). */
export const listVersions = (eventId: string) =>
    client
        .get<VersionList>('/api/homefeed/prep-runs', { params: { event_id: eventId } })
        .then(r => ({ ...r.data, pending: readPendingBuild(r.data?.pending) }));

/** How a background build is going. Owner (or admin) only; 404 when the
 *  build is unknown. */
export const getBuildStatus = (runId: string): Promise<BuildStatus> =>
    client.get<unknown>(`${runPath(runId)}/status`).then(r => readBuildStatus(r.data, runId));

/** One specific version, read-only unless it is the latest. */
export const getVersion = (runId: string) =>
    client.get<MeetingPrepData>(runPath(runId)).then(r => r.data);

/** Start a full regeneration in the background → the build that will become
 *  the new latest version (or the one already in flight for this meeting).
 *  `keepEdits` carries the sections the person edited by hand into it
 *  unchanged. */
export const regenerate = ({
    eventId,
    focusOn,
    keepEdits = true,
}: {
    eventId: string;
    focusOn?: string | null;
    keepEdits?: boolean;
}): Promise<PendingBuild> =>
    client
        .post<unknown>('/api/homefeed/prep-runs/regenerate', {
            event_id: eventId,
            focus_on: focusOn?.trim() || null,
            keep_edits: keepEdits,
        })
        .then(r => {
            const build = readAcceptedBuild(r.data);
            if (!build) throw new Error("Cubie couldn't start a new version. Your current version is unchanged.");
            return build;
        });

/** Save a hand edit of one section → the new latest version. */
export const editSection = (runId: string, key: SectionKey, value: SectionValue) =>
    client
        .put<MeetingPrepData>(`${runPath(runId)}/sections/${encodeURIComponent(key)}`, { value })
        .then(r => r.data);

/** Have Cubie rewrite one section (optionally to an instruction) → the new
 *  latest version. */
export const rewriteSection = (runId: string, key: SectionKey, instruction?: string | null) =>
    client
        .post<MeetingPrepData>(
            `${runPath(runId)}/sections/${encodeURIComponent(key)}/rewrite`,
            { instruction: instruction?.trim() || null },
            { timeout: GENERATION_TIMEOUT_MS },
        )
        .then(r => r.data);

/** Make an older version the latest again (appends a copy; nothing is lost).
 *  Restoring the latest returns it unchanged. */
export const restoreVersion = (runId: string) =>
    client.post<MeetingPrepData>(`${runPath(runId)}/restore`).then(r => r.data);

/** Editable Word twin of a version (blob → caller downloads). */
export const downloadDocx = (runId: string) =>
    client
        .get<Blob>(`/api/homefeed/prep-docs/${encodeURIComponent(runId)}/docx`, { responseType: 'blob' })
        .then(r => r.data);

/** Persist a version as a Word doc on the caller's Assets page. */
export const saveToAssets = (runId: string) =>
    client
        .post<{ doc_id?: string; created: boolean; filename?: string }>(
            `/api/homefeed/prep-docs/${encodeURIComponent(runId)}/save-to-assets`,
        )
        .then(r => r.data);

/** "meeting-prep-quarterly-review-v3.docx" — matches the server's name. */
export function docxFilename(title: string | null | undefined, version: number | null | undefined): string {
    const slug = (title || 'meeting')
        .replace(/[^a-zA-Z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .toLowerCase()
        .slice(0, 60);
    return `meeting-prep-${slug || 'meeting'}${version ? `-v${version}` : ''}.docx`;
}
