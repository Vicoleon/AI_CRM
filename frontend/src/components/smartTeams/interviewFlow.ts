// Pure decision logic for the Smart Teams interview page.
//
// WHY A SEPARATE MODULE
// ---------------------
// A 155-item interview has a handful of decisions that are worth being sure
// about — which page to land on when someone resumes mid-bank, what counts as
// unsaved, what to do with answers that were typed after the session token
// died. None of them need React, and the repo has no component test runner, so
// they live here as pure functions with type-only imports and are covered by
// `interviewFlow.test.ts` on Node's built-in runner:
//
//   cd <repo>/frontend && node --test "src/components/smartTeams/**/*.test.ts"
//
// (No test framework: `npm test` is Node's runner with TypeScript stripping,
// so this stays a leaf module with type-only imports.)
//
// THE OUTBOX
// ----------
// Answers are persisted server-side — `st_interviews` carries the cursor and the
// answers blob, so a refresh is a re-GET and nothing is held hostage in React
// state. The one hole that leaves is the debounce window: the shared axios
// interceptor hard-navigates to /login the moment a 401 comes back, which
// destroys component state without asking, and a 5h session JWT can die at item
// 96 of the Big Five bank. So every answer is ALSO written to a small
// localStorage outbox the instant it is given, and retired from it only when the
// server confirms the key. On the next mount of the same interview the outbox is
// replayed before anything renders. It is a crash log, not a cache: it is never
// read as the source of truth for what has been answered.
import type { AnswerValue, SectionKey } from '../../api/smartTeams';

/** Items per page in a scale bank.
 *
 *  Ten is a compromise between two real costs: a 50-item bank on one page is a
 *  wall that people abandon, and one item per page is 155 round trips of
 *  scrolling. Ten fits a laptop viewport with the anchor legend pinned above it,
 *  so the scale stays visible while answering — which is the thing that makes a
 *  bank fast. */
export const PAGE_SIZE = 10;

/** How long after the last keystroke or click a save flush fires. Short enough
 *  that a browser crash costs at most a couple of answers, long enough that
 *  rattling down a page of ten items is one request rather than ten. */
export const SAVE_DEBOUNCE_MS = 900;

/** Number of automatic retries before a failed save becomes the person's
 *  problem (a visible banner with a Retry button). Two, because the failures
 *  worth retrying silently are transient network blips; anything that survives
 *  two attempts needs to be seen, not swallowed. */
export const SAVE_MAX_AUTO_RETRIES = 2;

// The old global key is deliberately not read: it has no proof of who or
// which workspace wrote its pending answers.
export const OUTBOX_STORAGE_KEY = 'cubie_st_interview_outbox';

export interface OutboxScope {
    workspaceSlug: string;
    userEmail: string;
}

export function outboxStorageKey(scope: OutboxScope | null): string | null {
    const slug = scope?.workspaceSlug?.trim().toLowerCase();
    const email = scope?.userEmail?.trim().toLowerCase();
    if (!slug || !email) return null;
    return `${OUTBOX_STORAGE_KEY}:${encodeURIComponent(slug)}:${encodeURIComponent(email)}`;
}

// ── Paging within a scale bank ───────────────────────────────────────────────

/** How many pages a bank of `itemCount` items needs. Always at least one, so an
 *  empty bank still renders a page rather than dividing by zero downstream. */
export function pageCount(itemCount: number, pageSize: number = PAGE_SIZE): number {
    if (itemCount <= 0) return 1;
    return Math.ceil(itemCount / pageSize);
}

/** Clamp a page number that came from the URL. `?page=` is user-editable and
 *  survives a catalog change, so `page=99` on a 5-page bank must land on page 5
 *  rather than render nothing. Pages are 1-based in the URL. */
export function clampPage(requested: unknown, itemCount: number, pageSize: number = PAGE_SIZE): number {
    const total = pageCount(itemCount, pageSize);
    const parsed = typeof requested === 'number' ? requested : Number.parseInt(String(requested ?? ''), 10);
    if (!Number.isFinite(parsed) || parsed < 1) return 1;
    return Math.min(Math.trunc(parsed), total);
}

/** The 1-based page an item index sits on. */
export function pageForIndex(index: number, pageSize: number = PAGE_SIZE): number {
    if (index <= 0) return 1;
    return Math.floor(index / pageSize) + 1;
}

/** The slice of items shown on a 1-based page. */
export function pageSlice<T>(items: readonly T[], page: number, pageSize: number = PAGE_SIZE): T[] {
    const start = (Math.max(1, page) - 1) * pageSize;
    return items.slice(start, start + pageSize);
}

// ── Answered-ness ────────────────────────────────────────────────────────────

/** Whether a stored value counts as an answer.
 *
 *  `null` does not: `interview.validate_answers` accepts null as "asked and
 *  cleared", and the Python progress calculation counts only non-null values.
 *  Counting a cleared answer would show a full bank the person cannot submit. */
export function isAnswered(value: AnswerValue | undefined): boolean {
    return value !== undefined && value !== null;
}

export function answeredCount(
    itemIds: readonly string[],
    answers: Readonly<Record<string, AnswerValue>>,
): number {
    return itemIds.reduce((n, id) => (isAnswered(answers[id]) ? n + 1 : n), 0);
}

/** The first item with no answer, or null when the bank is complete. This is
 *  what makes a resume land mid-section instead of at the top of it. */
export function firstUnansweredIndex(
    itemIds: readonly string[],
    answers: Readonly<Record<string, AnswerValue>>,
): number | null {
    const index = itemIds.findIndex(id => !isAnswered(answers[id]));
    return index === -1 ? null : index;
}

/** The page to open a bank on: where they stopped, or the last page when every
 *  item is answered (so the Continue button is in view, not a page away). */
export function resumePage(
    itemIds: readonly string[],
    answers: Readonly<Record<string, AnswerValue>>,
    pageSize: number = PAGE_SIZE,
): number {
    const next = firstUnansweredIndex(itemIds, answers);
    if (next === null) return pageCount(itemIds.length, pageSize);
    return pageForIndex(next, pageSize);
}

/** Item ids on this page that still have no answer — what the "3 left on this
 *  page" hint counts, and what the Continue guard checks. */
export function unansweredOnPage(
    itemIds: readonly string[],
    answers: Readonly<Record<string, AnswerValue>>,
): string[] {
    return itemIds.filter(id => !isAnswered(answers[id]));
}

// ── Keyboard ─────────────────────────────────────────────────────────────────

/** Map a digit keypress to a scale value, or null when it is not one.
 *
 *  Native radio inputs already give arrow-key movement and selection for free,
 *  which is why the banks use them; digits are the accelerator on top, so that
 *  answering a page is "3 4 2 5 …" without leaving the home row. Bounds come
 *  from the section (`value_min`/`value_max`), never from an assumption that
 *  every scale is 1-5. */
export function digitToScaleValue(key: string, min: number, max: number): number | null {
    if (key.length !== 1 || key < '0' || key > '9') return null;
    const value = Number(key);
    if (value < min || value > max) return null;
    return value;
}

/** Map a keypress to a forced-choice answer: A/B, or 1/2 for the same fingers
 *  the likert banks use. */
export function keyToChoice(key: string): 'a' | 'b' | null {
    const lower = key.toLowerCase();
    if (lower === 'a' || lower === '1') return 'a';
    if (lower === 'b' || lower === '2') return 'b';
    return null;
}

// ── Formatting ───────────────────────────────────────────────────────────────

/** Minutes, rounded up, phrased for a person. Rounding up on purpose: a
 *  progress hint that under-promises is the kinder error. */
export function formatMinutes(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return 'less than a minute';
    const minutes = Math.ceil(seconds / 60);
    if (minutes === 1) return 'about a minute';
    if (minutes < 60) return `about ${minutes} minutes`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    const hourPart = hours === 1 ? 'about an hour' : `about ${hours} hours`;
    return rest ? `${hourPart} ${rest} min` : hourPart;
}

/** What the save indicator says. The failed state is deliberately wordy: a save
 *  that silently dropped answers is the one failure this page must never have. */
export type SaveState = 'idle' | 'saving' | 'saved' | 'failed';

export function saveStateLabel(state: SaveState, unsavedCount: number): string {
    switch (state) {
        case 'saving':
            return 'Saving…';
        case 'saved':
            return 'Saved';
        case 'failed':
            return unsavedCount === 1
                ? "1 answer hasn't saved"
                : `${unsavedCount} answers haven't saved`;
        case 'idle':
            return unsavedCount ? 'Not saved yet' : 'Saved';
    }
}

// ── The outbox ───────────────────────────────────────────────────────────────

export interface Outbox {
    interview_id: string;
    section_cursor: SectionKey;
    answers: Record<string, AnswerValue>;
}

/** Add pending answers to an outbox, replacing values for keys already in it.
 *
 *  An outbox belonging to a different interview is discarded rather than merged:
 *  after a retake the old row's item ids are the same strings, and replaying them
 *  into the new interview would import answers the person did not give this
 *  time. */
export function mergeOutbox(
    existing: Outbox | null,
    interviewId: string,
    sectionCursor: SectionKey,
    answers: Readonly<Record<string, AnswerValue>>,
): Outbox {
    const base = existing && existing.interview_id === interviewId ? existing.answers : {};
    return {
        interview_id: interviewId,
        section_cursor: sectionCursor,
        answers: { ...base, ...answers },
    };
}

/** Drop keys the server has confirmed. Returns null when nothing is left, which
 *  is the signal to clear the stored entry entirely — an empty outbox on disk
 *  reads as "a save failed" on the next mount. */
export function retireFromOutbox(
    existing: Outbox | null,
    savedKeys: readonly string[],
): Outbox | null {
    if (!existing) return null;
    const remaining: Record<string, AnswerValue> = {};
    for (const [key, value] of Object.entries(existing.answers)) {
        if (!savedKeys.includes(key)) remaining[key] = value;
    }
    if (!Object.keys(remaining).length) return null;
    return { ...existing, answers: remaining };
}

/** Whether a stored outbox is worth replaying into `interviewId`. */
export function outboxAppliesTo(existing: Outbox | null, interviewId: string | null): boolean {
    return (
        !!existing &&
        !!interviewId &&
        existing.interview_id === interviewId &&
        Object.keys(existing.answers).length > 0
    );
}

function isOutbox(value: unknown): value is Outbox {
    if (!value || typeof value !== 'object') return false;
    const candidate = value as Partial<Outbox>;
    return (
        typeof candidate.interview_id === 'string' &&
        typeof candidate.section_cursor === 'string' &&
        !!candidate.answers &&
        typeof candidate.answers === 'object'
    );
}

/** Read the outbox. Every failure path returns null: a corrupt entry (hand-edited,
 *  or written by an older shape of this file) must not be able to stop the
 *  interview page from mounting. */
export function readOutbox(scope: OutboxScope | null): Outbox | null {
    const storageKey = outboxStorageKey(scope);
    if (typeof localStorage === 'undefined' || !storageKey) return null;
    try {
        const raw = localStorage.getItem(storageKey);
        if (!raw) return null;
        const parsed: unknown = JSON.parse(raw);
        return isOutbox(parsed) ? parsed : null;
    } catch {
        return null;
    }
}

export function writeOutbox(outbox: Outbox | null, scope: OutboxScope | null): void {
    const storageKey = outboxStorageKey(scope);
    if (typeof localStorage === 'undefined' || !storageKey) return;
    try {
        if (!outbox) {
            localStorage.removeItem(storageKey);
            return;
        }
        localStorage.setItem(storageKey, JSON.stringify(outbox));
    } catch {
        // A full or blocked localStorage costs us the crash log, not the
        // interview: the server write is still the real persistence.
    }
}
