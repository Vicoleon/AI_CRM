// Smart Teams API client. Uses the shared axios instance (bearer token +
// request id attached by its interceptor); full /api/... paths, no baseURL.
//
// COVERS ALL 37 SMART TEAMS ROUTES, across six routers:
//   backend/routers/smart_teams.py            — the Work Style Interview (11)
//   backend/routers/smart_teams_guides.py     — the personal guide, ST-6 (7)
//   backend/routers/smart_teams_uploads.py    — official reports, ST-5 (6)
//   backend/routers/smart_teams_skills.py     — skills, ST-8 (8)
//   backend/routers/smart_teams_comparison.py — the pair guide, ST-7 (3)
//   backend/routers/smart_teams_teams.py      — Team Builder, ST-9 (2)
// The interview half is first; everything from ST-5 onwards is in the WAVE 2
// block at the bottom, which carries its own "what the routers do not offer".
//
// THE ROUTER IS THE AUTHORITY
// ---------------------------
// Every type and every path below is transcribed from the router that serves it,
// field name by field name. The previous cut of this file was written
// against a guess of what the router would look like, and the guess lost: seven
// of its nine calls named endpoints that do not exist, and every answer-save
// went to `PATCH /interview/answers`, a path that exists only for GET. Somebody
// could have answered 155 items over half an hour with every save 405-ing while
// the page looked fine.
//
// `backend/tests/test_smart_teams_api_contract.py` now reads THIS FILE off disk,
// pulls out every path and method, and asserts each one exists on the built
// FastAPI app. So: change a path here only alongside the router, and never add a
// call to an endpoint that is not mounted — the test names the offending
// path+method and fails.
//
// WHAT THE ROUTER DOES NOT OFFER (gaps, not omissions)
// ----------------------------------------------------
//   * There is no overview/summary endpoint. The landing page is built from
//     `GET /interview` plus `GET /interview/status`.
//   * `profile` is a version stub — id, version, status, origin, scored_at,
//     provenance. There are NO per-framework layer summaries, no headline prose
//     and no four-letter type anywhere in this file, so nothing here can render
//     a bare "ENFJ": the only way to add one is to add a field, and it must
//     arrive with its derived qualifier beside it (`type16.NOT_AN_MBTI_RESULT`
//     is the rule).
//   * There is no raw score on any interview response, and no interview read
//     other than `/support/interviews/{email}` takes an email — every one of
//     them is scoped to the caller by the server. That support read is a support
//     admin's debugging read (`can_view_raw_profile`) and is wrapped below as
//     `supportReadInterview`; gate any UI for it on `canViewSmartTeamsRawData`.
//   * Section completion is NOT a POST of its own: it is `complete: true` on the
//     section save (or `skip: true` for an optional section), and for the three
//     conversational sections it is decided by the runtime inside the turn.
import client from './client';

// ── Vocabularies (mirrors of the Python literals) ────────────────────────────

/** `interview.SECTION_*`, in presentation order. These strings are stored in
 *  `st_interviews`, so a typo here is a section that can never be completed. */
export type SectionKey =
    | 'disc_likert'
    | 'disc_forced_choice'
    | 'big_five'
    | 'strengths'
    | 'motivators'
    | 'motivators_probe'
    | 'enneagram'
    | 'enneagram_confirmation'
    | 'skills';

/** `interview.SectionKind` — how a section is presented. */
export type SectionKind = 'scale_bank' | 'conversational' | 'skills_declaration';

/** `interview.AnswerKind` — what a stored value is, and THE discriminator the
 *  page switches on. Every branch of `_section_payload` stamps it. */
export type AnswerKind =
    | 'likert'
    | 'choice_ab'
    | 'transcript'
    | 'skill_level'
    | 'structured';

/** `database.smart_teams.SKILL_LEVELS`. */
export type SkillLevel = 'familiar' | 'working' | 'strong' | 'expert';

/** `strengths.Speaker`. Attribution is the evidence rule for the strengths
 *  layer: only an `interviewee` turn can ever be quoted in a guide. */
export type Speaker = 'interviewee' | 'cubie';

export interface Turn {
    speaker: Speaker;
    text: string;
    /** The scripted prompt this turn answers, when the interview knows it. */
    prompt_id: string | null;
}

/** The stored `meta:enneagram_confirmation` value. Written by the server from
 *  the option the person picked (mapped back to a type BY POSITION); read here
 *  only to show that the section has been answered. */
export interface EnneagramConfirmationAnswer {
    type_number: number | null;
    wing: number | null;
    declined: boolean;
}

/** One value in an `st_interviews.answers` blob. `null` means "asked, cleared"
 *  — distinct from a key that was never sent. */
export type AnswerValue =
    | number
    | 'a'
    | 'b'
    | SkillLevel
    | Turn[]
    | EnneagramConfirmationAnswer
    | null;

/** Answer-key namespaces, from `interview.PREFIX_*`. A key with no prefix is an
 *  instrument item id. The client only ever WRITES `skill:` keys — transcripts
 *  and the confirmation blob are written server-side by the turn endpoint. */
export const PREFIX_SKILL = 'skill:';
export const PREFIX_META = 'meta:';

// ── Interview state ──────────────────────────────────────────────────────────

/** `st_interviews.status`, plus `not_started` for "this person has no row". */
export type InterviewStatus = 'not_started' | 'in_progress' | 'completed' | 'abandoned';

/** `interview.Progress`, exactly as `_progress_dict` ships it.
 *
 *  `percent` is time-weighted, not section-weighted — nine sections range from
 *  75 seconds to ten minutes, so an even ninth per section would lie. There is
 *  no `seconds_remaining`: the server sends `minutes_remaining` and the previous
 *  client read a field that never arrived (rendering `NaN`). */
export interface InterviewProgress {
    sections_total: number;
    sections_done: number;
    steps_total: number;
    steps_done: number;
    seconds_total: number;
    seconds_done: number;
    percent: number;
    minutes_remaining: number;
    current_section_key: SectionKey | null;
    is_complete: boolean;
}

/** `_section_dict` — the catalog record for one section. */
export interface SectionMeta {
    key: SectionKey;
    title: string;
    kind: SectionKind;
    answer_kind: AnswerKind;
    blurb: string;
    optional: boolean;
    feeds: string[];
    item_ids: string[];
    prompt_ids: string[];
    value_min: number | null;
    value_max: number | null;
    estimated_seconds: number;
    step_count: number;
}

/** The two notices, verbatim from the server. `advisory_notice` is
 *  `disc.ADVISORY_NOTICE` — the sentence that says these answers are never used
 *  for performance review, promotion or compensation — and `privacy_notice`
 *  states what `authz.can_view_raw_profile` actually enforces. Rendered as
 *  received; a paraphrase written in the browser would be a promise nobody
 *  reviewed. Sent by `/interview`, `/interview/start` and `/interview/retake`
 *  only — `/interview/status` is a cheap poll and carries neither. */
export interface Notices {
    advisory_notice: string;
    privacy_notice: string;
}

// ── Section payloads (normalized by the router) ───────────────────────────────

export interface ScalePoint {
    value: number;
    label: string;
}

export interface LikertItem {
    item_id: string;
    prompt: string;
}

export interface ForcedChoiceItem {
    item_id: string;
    option_a: string;
    option_b: string;
}

export interface LikertPayload {
    answer_kind: 'likert';
    /** The anchor wording for THIS layer. DISC, Big Five, motivators and the
     *  Enneagram each word their five points differently, so anchors are
     *  rendered from here and never hardcoded in a component. */
    scale: ScalePoint[];
    items: LikertItem[];
    /** `big_five.ITEM_STEM` ("I…") — present on that bank only. */
    stem?: string;
}

export interface ChoicePayload {
    answer_kind: 'choice_ab';
    /** `disc.FORCED_CHOICE_PROMPT`. */
    instruction: string;
    items: ForcedChoiceItem[];
}

/** `_conversation_dict` — one scripted segment's live state.
 *
 *  `transcript` is speaker-attributed on purpose (`contracts.transcript_to_json`):
 *  the strengths extraction checks every evidence quote against the interviewee's
 *  own turns, and a flattened transcript would let a company-visible guide print
 *  Cubie's question back at somebody under "in your words". */
export interface ConversationState {
    section_key: SectionKey;
    transcript: Turn[];
    pending_question: string | null;
    awaiting_answer: boolean;
    finished: boolean;
    closing_line: string | null;
    question_index: number;
    questions_total: number;
    probes_offered: number;
    max_probes_per_question: number;
}

export interface TranscriptPayload {
    answer_kind: 'transcript';
    /** False when the segment cannot run yet — the motivators probe is worded
     *  from the SCORED motivators bank, so it is unavailable until that section
     *  is complete. `conversation` is null exactly then, and the reason is the
     *  server's own sentence. Never render an empty question instead. */
    available: boolean;
    unavailable_reason: string | null;
    turn_endpoint: string;
    prompts: { prompt_id: string; question: string }[];
    conversation: ConversationState | null;
}

export interface TypeCandidate {
    type_number: number;
    label: string;
    plain_language: string;
}

/** `conversation.ConfirmationPresentation.as_dict()` plus provenance.
 *
 *  `options` MUST be rendered in the order received: `enneagram_answer` maps the
 *  chosen option back to a type BY POSITION. `provenance` / `confidence` /
 *  `answered_items` describe where a *suggestion* came from and are deliberately
 *  not rendered as a finding — with zero answers, `question` is the instrument's
 *  own "not enough answers to suggest a type" sentence and `candidates` is
 *  empty. */
export interface ConfirmationState {
    required: boolean;
    question: string;
    candidates: TypeCandidate[];
    options: string[];
    footnote: string;
    answered_items: number;
    provenance: string;
    confidence: string;
    inferred_wing: number | null;
}

export interface StructuredPayload {
    answer_kind: 'structured';
    available: boolean;
    unavailable_reason: string | null;
    turn_endpoint: string;
    answer_keys: string[];
    /** All nine patterns, for the "let me pick from the nine" option. */
    types: { number: number; label: string }[];
    confirmation: ConfirmationState | null;
}

export interface SkillOption {
    skill_key: string;
    name: string;
    cluster: string | null;
}

export interface SkillPayload {
    answer_kind: 'skill_level';
    /** `SKILL_LEVELS`, as bare strings. */
    levels: SkillLevel[];
    /** Active `st_skills_taxonomy` rows — empty until ST-8 seeds it, which the
     *  page has to say out loud rather than render as an empty list. */
    skills: SkillOption[];
}

export type SectionPayload =
    | LikertPayload
    | ChoicePayload
    | TranscriptPayload
    | StructuredPayload
    | SkillPayload;

/** `_section_read` — one section as the page renders it. The field is `payload`,
 *  not `content`: that rename is the single-word version of this whole fix. */
export interface SectionView {
    interview_id: string | null;
    interview_status: InterviewStatus | null;
    section: SectionMeta;
    payload: SectionPayload;
    /** Only this section's answers, already saved. */
    answers: Record<string, AnswerValue>;
    progress: InterviewProgress;
    is_completed: boolean;
    first_unanswered_item: string | null;
}

/** The turn response: a fresh section read plus what the turn decided. */
export interface TurnResult extends SectionView {
    /** False when the segment had already run out of questions — reported
     *  rather than refused, so one turn too many is a 200. */
    accepted: boolean;
    section_completed: boolean;
    sections_completed: SectionKey[];
    current_section: SectionMeta | null;
    ready_to_complete: boolean;
    caveats: string[];
}

// ── Resume state ─────────────────────────────────────────────────────────────

/** `_resume_state` — what every read of one's own in-progress interview returns. */
export interface ResumeState {
    interview_id: string;
    status: InterviewStatus;
    sections_completed: SectionKey[];
    progress: InterviewProgress;
    current_section: SectionMeta | null;
    /** The raw stored cursor. */
    section_cursor: SectionKey | null;
    /** The RESOLVED cursor (`interview.resume_target`) — navigate to this. They
     *  differ whenever the stored cursor points at a section that is already
     *  finished, and the server is where that race is settled. */
    resume_section_key: SectionKey | null;
    section_answers: Record<string, AnswerValue>;
    first_unanswered_item: string | null;
    started_at: string | null;
    last_activity_at: string | null;
    completed_at: string | null;
    profile_id: string | null;
    sections: SectionMeta[];
    /** Omitted by `/interview/answers` and the support read. */
    current_section_payload?: SectionPayload;
}

// ── Scoring and profile ──────────────────────────────────────────────────────

/** The scoring vocabulary the router actually has a database behind.
 *
 *  There is no `queued`, `running` or `failed`, no `run_id` and no `error`:
 *  `st_interviews` has no failure column, so a run that never came back is
 *  reported as `delayed` BY AGE and nothing else may be expected. A client that
 *  polls for `running` polls forever. */
export type ScoringPhase = 'not_started' | 'pending' | 'delayed' | 'ready';

export interface ScoringStatus {
    status: ScoringPhase;
    /** Always present except on the "no interview row at all" shape. */
    job_module?: string;
    /** Only when `delayed`, and it is the copy to show the person. */
    message?: string;
    /** Only on the `POST /interview/complete` response. */
    executor?: string;
    execution_name?: string;
}

/** `_profile_dict`. A version stub — deliberately no layers, no prose and no
 *  scores. See the header. */
export interface ProfileSummary {
    id: string;
    version: number;
    status: 'draft' | 'current' | 'superseded';
    origin: string;
    scored_at: string | null;
    provenance: Record<string, unknown>;
}

/** `_completion_state` for a person who has a row. */
export interface CompletionState {
    status: 'in_progress' | 'completed' | 'abandoned';
    interview_id: string;
    completed_at: string | null;
    scoring: ScoringStatus;
    profile: ProfileSummary | null;
}

/** `_completion_state` for a person who does not. */
export interface NoInterviewState {
    status: 'not_started';
    scoring: { status: 'not_started' };
}

export type InterviewStatusResponse = CompletionState | NoInterviewState;

// ── Endpoint response shapes ─────────────────────────────────────────────────

/** `GET /interview`, never-started branch. Note: no `progress`, no
 *  `interview_id` — reading either would be reading a field that is not there. */
export interface NotStartedView extends Notices {
    status: 'not_started';
    sections: SectionMeta[];
    /** `interview.duration_report().required_minutes` — the honest figure
     *  (29.4 minutes), still over the ~25 minute target. The page prints what
     *  this says, not the target, and reads it from the API so a further trim
     *  needs no change here. */
    estimated_minutes: number;
    can_retake: false;
}

export interface InProgressView extends Notices, ResumeState {
    status: 'in_progress';
}

export interface CompletedView extends Notices, CompletionState {
    status: 'completed';
    can_retake: boolean;
}

export type InterviewView = NotStartedView | InProgressView | CompletedView;

export type StartResponse =
    | (InProgressView & { created: boolean; can_retake: false })
    | (CompletedView & { created: false; can_retake: true });

export type RetakeResponse = InProgressView & {
    created: boolean;
    previous_interview_id: string | null;
    can_retake: false;
};

export interface SaveResponse extends ResumeState {
    saved_section: SectionKey;
    /** Echoed back so the client retires exactly what landed, rather than
     *  assuming the whole batch did. */
    saved_keys: string[];
    section_completed: boolean;
    skipped: boolean;
    /** Disclosures — e.g. a clipped answer. Shown, not swallowed. */
    caveats: string[];
    ready_to_complete: boolean;
}

export interface CompleteResponse extends CompletionState {
    enqueued: boolean;
    auto_skipped_sections: SectionKey[];
}

export type AbandonResponse =
    | { status: 'abandoned'; interview_id: string }
    | { status: 'none' };

export interface SectionSaveBody {
    answers: Record<string, AnswerValue>;
    /** A request, not an instruction: the server completes the section only if
     *  the answers on the row support it. */
    complete?: boolean;
    /** Legal only for a section the catalog marks `optional`. */
    skip?: boolean;
}

/** One turn. `answer` for the two scripted segments (an empty string is a
 *  legitimate "nothing to say" skip); `option` — the exact string that was
 *  offered — for the Enneagram confirmation, plus `type_number` when the option
 *  is the "let me pick from the nine" one. */
export interface TurnBody {
    answer?: string;
    option?: string;
    type_number?: number | null;
    wing?: number | null;
}

// ── Errors ───────────────────────────────────────────────────────────────────

export interface SmartTeamsError {
    code: string | null;
    message: string;
    status: number | null;
}

interface MaybeAxiosError {
    message?: string;
    response?: { status?: number; data?: { detail?: unknown } };
}

/** Flatten an axios failure into something renderable.
 *
 *  Note what does NOT happen on a 403: the shared response interceptor only
 *  reacts to 401, deliberately ("only 401 means 'token bad'"), so a Smart Teams
 *  gate refusal arrives here as an ordinary error and the page has to render its
 *  own denial state. */
export function smartTeamsError(
    e: unknown,
    fallback = 'Smart Teams could not be reached.',
): SmartTeamsError {
    const err = (e ?? {}) as MaybeAxiosError;
    const status = err.response?.status ?? null;
    const detail = err.response?.data?.detail;
    if (detail && typeof detail === 'object') {
        const obj = detail as { code?: string | null; message?: string };
        return { code: obj.code ?? null, message: obj.message || fallback, status };
    }
    return {
        code: null,
        message: (typeof detail === 'string' && detail) || err.message || fallback,
        status,
    };
}

// ── Calls ────────────────────────────────────────────────────────────────────
//
// One function per route, and every path below is asserted against the built app
// by `backend/tests/test_smart_teams_api_contract.py`.

/** Where the caller is in their own interview. Never creates a row. */
export async function getInterview(): Promise<InterviewView> {
    const { data } = await client.get<InterviewView>('/api/smart-teams/interview');
    return data;
}

/** Start the interview, or hand back the one already in progress.
 *
 *  Idempotent: the interview page calls it on every mount, including a hard
 *  refresh mid-interview, and an open row comes back as-is. A COMPLETED
 *  interview is never reopened — the response is the completion shape with
 *  `can_retake: true`, and `retakeInterview` is what acts on that. */
export async function startInterview(): Promise<StartResponse> {
    const { data } = await client.post<StartResponse>('/api/smart-teams/interview/start');
    return data;
}

/** Take it again: a NEW row, with the finished one and its scored profile left
 *  exactly as they are. 201. 409 when there is nothing finished to retake. */
export async function retakeInterview(): Promise<RetakeResponse> {
    const { data } = await client.post<RetakeResponse>('/api/smart-teams/interview/retake');
    return data;
}

/** One section: its payload, this person's answers to it, and the progress bar. */
export async function getSection(key: SectionKey): Promise<SectionView> {
    const { data } = await client.get<SectionView>(`/api/smart-teams/interview/sections/${key}`);
    return data;
}

/** Merge answers into one section. PUT — not the PATCH the previous client sent
 *  to a GET-only path. Safe to repeat: answers are merged by key. */
export async function saveSection(
    key: SectionKey,
    body: SectionSaveBody,
): Promise<SaveResponse> {
    const { data } = await client.put<SaveResponse>(
        `/api/smart-teams/interview/sections/${key}`,
        body,
    );
    return data;
}

/** Advance one conversational section by one exchange.
 *
 *  NOT idempotent, and it cannot be: a blind retry is a second answer to the
 *  next question. On a failure the page must re-read the section (`getSection`)
 *  rather than send this again. */
export async function takeTurn(key: SectionKey, body: TurnBody): Promise<TurnResult> {
    const { data } = await client.post<TurnResult>(
        `/api/smart-teams/interview/sections/${key}/turn`,
        body,
    );
    return data;
}

/** Close the interview and hand scoring to cubie-worker. 202. */
export async function completeInterview(): Promise<CompleteResponse> {
    const { data } = await client.post<CompleteResponse>('/api/smart-teams/interview/complete');
    return data;
}

/** Abandon the open interview. The row is kept as `abandoned` — the answers
 *  somebody already gave are theirs — so this is "set this attempt aside", not
 *  "delete it". Repeating it is a no-op. */
export async function abandonInterview(): Promise<AbandonResponse> {
    const { data } = await client.post<AbandonResponse>('/api/smart-teams/interview/abandon');
    return data;
}

/** The scoring poll. Cheap by design: two rows, no answers, no notices. */
export async function getInterviewStatus(): Promise<InterviewStatusResponse> {
    const { data } = await client.get<InterviewStatusResponse>(
        '/api/smart-teams/interview/status',
    );
    return data;
}

// ═════════════════════════════════════════════════════════════════════════════
// WAVE 2 — guides (ST-6), uploads (ST-5), skills (ST-8), comparison (ST-7),
// Team Builder (ST-9).
//
// Same discipline as everything above: every path and every field name below is
// transcribed from the router that serves it, not inferred from the path.
//   * guides      → backend/routers/smart_teams_guides.py
//   * uploads     → backend/routers/smart_teams_uploads.py
//   * skills      → backend/routers/smart_teams_skills.py
//   * comparison  → backend/routers/smart_teams_comparison.py
//   * teams       → backend/routers/smart_teams_teams.py
//
// WHAT THE ROUTERS DO NOT OFFER (gaps, stated so nobody builds against a wish)
//   * No "generate a guide for somebody else", no `subject_a` on the pair
//     routes, and no `requested_by` on the Team Builder body. The subject/reader
//     is always the authenticated caller, by absence of a parameter.
//   * There is NO approve, unpublish, or .docx export for a pair guide, and no
//     Save-to-Assets for any guide. Each absence is a decision documented in the
//     router; do not add a client function for one.
//   * Team Builder returns nothing to persist and has no GET — the answer is the
//     POST response. There is no history of past asks.
//   * `POST /skills/refresh` is fire-and-forget: it returns `status: "queued"`
//     and there is no per-run poll. Re-read `GET /skills` to see what landed.
//   * A generate/upload/refresh reports what it DISPATCHED, never a result. The
//     row's own `status` (or `review_status`) is the only truth about progress,
//     so every one of these surfaces polls its read endpoint.
// ═════════════════════════════════════════════════════════════════════════════

/** `database.smart_teams.DOCUMENT_STATUSES`. There is no `queued` and no
 *  `running`: a dispatched job that never came back leaves the row `pending`
 *  forever, so a page must show `pending` as "still being written" and never
 *  wait for a state that cannot arrive. */
export type DocumentStatus = 'pending' | 'ready' | 'failed';

/** `database.smart_teams.PROFILE_FRAMEWORKS` — the six layers a profile can
 *  hold. Used for provenance keys and upload payloads. */
export type ProfileFramework =
    | 'disc'
    | 'big_five'
    | 'type16'
    | 'strengths'
    | 'motivators'
    | 'enneagram';

/** `database.smart_teams.PROVENANCE_SOURCES`. */
export type ProvenanceSource = 'interview' | 'official_upload' | 'merged';

// ── ST-6: the personal working guide ─────────────────────────────────────────

/** `_document_dict` in `smart_teams_guides.py`.
 *
 *  `content_md` is present ONLY when the router was asked for the body:
 *  `GET /guides/me`, `GET /guides/{subject_email}`, the approve/unpublish
 *  responses, and a CACHED generate. It is absent on the support list and on a
 *  generate that actually enqueued — render from `status`, not from a body you
 *  assumed would be there.
 *
 *  `rag_ingested` is reported as-is, deliberately. On an unpublished row that
 *  still says true, the guide is out of the app and STILL reachable by Cubie
 *  Chat until the purge job lands, and the UI is allowed to say so.
 *
 *  `stale` is `guide.is_stale(row, current_profile_version)` — server-decided.
 *  Never recompute it by comparing the two version numbers here. */
export interface GuideDocument {
    id: string;
    kind: 'personal_guide';
    subject_email: string;
    display_name: string;
    title: string;
    status: DocumentStatus;
    /** The row's own failure sentence when `status` is `failed`. */
    error: string | null;
    /** The profile version this guide was WRITTEN from. */
    profile_version: number | null;
    /** The subject's live profile version now. */
    current_profile_version: number | null;
    stale: boolean;
    published: boolean;
    approved_at: string | null;
    approved_by: string | null;
    unpublished_at: string | null;
    rag_ingested: boolean;
    created_at: string | null;
    updated_at: string | null;
    advisory_notice: string;
    content_md?: string;
}

/** `GET /guides/me`. Always 200 — "you have no guide" is `state: 'none'`, not a
 *  404. `can_generate` is false until the interview has been scored. */
export interface OwnGuideResponse {
    state: 'none' | DocumentStatus;
    guide: GuideDocument | null;
    current_profile_version: number | null;
    can_generate: boolean;
    advisory_notice: string;
}

/** `GET /guides/{subject_email}` — a colleague's PUBLISHED guide, body included.
 *  404 both for "no published guide" and "no guide at all", on purpose: telling
 *  those apart would let anybody enumerate who has an unpublished draft. */
export interface ColleagueGuideResponse {
    guide: GuideDocument;
    advisory_notice: string;
}

/** `GET /guides/support/{subject_email}` — support admins only (`can_view_raw_profile`:
 *  the person themselves plus a support admin; an exec and a team manager are
 *  refused). Every generation for one person, drafts and failures included, and
 *  bodies deliberately OMITTED — this answers "why is it stuck", not "what does
 *  it say". */
export interface GuideSupportResponse {
    subject_email: string;
    display_name: string;
    current_profile_version: number | null;
    guides: GuideDocument[];
}

/** `POST /guides/generate` (202). Discriminated on `enqueued`:
 *  a cache hit returns the finished guide WITH its body and nothing was
 *  dispatched; an enqueue returns the row WITHOUT a body and the page polls
 *  `GET /guides/me`. */
export type GenerateGuideResponse =
    | {
          enqueued: false;
          reason: 'cached';
          guide: GuideDocument;
          advisory_notice: string;
      }
    | {
          enqueued: true;
          reason: 'generating';
          executor: string;
          execution_name: string;
          guide: GuideDocument;
          advisory_notice: string;
      };

/** One cubie-worker dispatch reported by `approve`. */
export interface GuideDispatch {
    action: 'ingest' | 'purge';
    document_id: string;
    executor: string;
    execution_name: string;
}

/** `POST /guides/{document_id}/approve`. Publication is synchronous; the corpus
 *  copy is not. `rag_ingested` is the row's value AT RESPONSE TIME and is
 *  therefore false on a first approve — say "publishing to Cubie Chat", never
 *  "published to Chat", until a later read says true.
 *
 *  `superseded` are the caller's previously published guides this approval
 *  retired; each ingested one gets a `purge` dispatch. */
export interface ApproveGuideResponse {
    guide: GuideDocument;
    superseded: string[];
    dispatched: GuideDispatch[];
    rag_ingested: boolean;
    advisory_notice: string;
}

/** `POST /guides/{document_id}/unpublish`. In-app reads stop immediately;
 *  `rag_purge` is `pending` until cubie-worker removes the corpus copy, and
 *  `done` when there was nothing to remove. Idempotent — a repeat is a 200 with
 *  `dispatched` absent. */
export interface UnpublishGuideResponse {
    guide: GuideDocument;
    rag_purge: 'pending' | 'done';
    dispatched?: { executor: string; execution_name: string } | null;
    advisory_notice: string;
}

// ── ST-5: the optional official-report upload ────────────────────────────────

/** `database.smart_teams.UPLOAD_REVIEW_STATUSES`. */
export type UploadReviewStatus = 'pending' | 'confirmed' | 'rejected' | 'merged';

/** `uploads._coerce_proposal`'s payload — numbers and closed-vocabulary labels
 *  only, proved by `_assert_no_prose`. The framework's own key holds the scores,
 *  and `disc` is the ONLY mergeable one today, so that is the only score key
 *  that can appear.
 *
 *  THESE ARE RAW SCORES. They may be rendered ONLY on the subject's own upload
 *  review screen (and the support read). Never on any colleague-facing
 *  surface. */
export interface ParsedUploadPayload {
    framework: ProfileFramework;
    /** `_ACCEPTED_SCALES`. A percentile-scaled report is refused, not rescaled. */
    scale: 'zero_to_100' | 'percent';
    /** `disc.DIMENSIONS` — D, I, S, C, all four or the parse refuses. 0-100. */
    disc?: Record<string, number>;
}

/** `uploads.upload_dict` plus the advisory notice the router adds.
 *
 *  `parsed` is null until the parse job has run, and stays null on every
 *  refusal. `parse_error` is OUR sentence from `uploads.PARSE_REASONS` — never a
 *  model string and never an excerpt of the uploaded document — so it is safe to
 *  render verbatim. `framework` can be set while `mergeable` is false: that is
 *  "we know what this report is and cannot use it", which is a different screen
 *  from "we could not tell what it is". */
export interface UploadRecord {
    id: string;
    original_filename: string;
    framework: string | null;
    parsed: ParsedUploadPayload | null;
    parse_error: string | null;
    review_status: UploadReviewStatus;
    mergeable: boolean;
    merged_profile_id: string | null;
    uploaded_at: string | null;
    parsed_at: string | null;
    reviewed_at: string | null;
    advisory_notice: string;
    /** Added by `GET /uploads/{id}` only, and only while the row is mergeable
     *  AND still `pending` — it is the consequence of pressing Confirm, and it
     *  belongs on the screen where that decision is made. */
    merge_notice?: string;
}

/** `GET /uploads`. `[]` is the normal, complete answer for almost everybody —
 *  `optional_notice` is the copy that says so, and nothing here may be rendered
 *  as a to-do or a gap. */
export interface UploadListResponse {
    uploads: UploadRecord[];
    optional_notice: string;
    /** `sorted(uploads.MERGE_ADAPTERS)` — today, exactly `['disc']`. */
    mergeable_frameworks: string[];
}

/** `POST /uploads` (202, multipart). The row exists; the proposal does not yet.
 *  Poll `GET /uploads/{id}`. 400 (with the library's own sentence) for a
 *  non-PDF, an empty file, or one over 10 MB. */
export interface CreateUploadResponse extends UploadRecord {
    optional_notice: string;
    parse: {
        status: 'pending';
        job_module: string;
        executor: string;
        execution_name: string;
    };
}

/** `uploads.REGENERATION_STATES` — a MEASUREMENT of the caller's guide after a
 *  merge, never a claim that a regeneration was requested. Nothing queues one;
 *  the person regenerates from the guide page. */
export type GuideRegenerationState = 'unavailable' | 'no_guide' | 'stale' | 'current';

/** `POST /uploads/{upload_id}/confirm`. The merge is synchronous and writes ONE
 *  new profile version.
 *
 *  `guide.purged_document_ids` and `guide.still_ingested_document_ids` are split
 *  on purpose: a guide can be out of company view and still answerable in Cubie
 *  Chat. One count would let the second failure hide behind the first success. */
export interface ConfirmUploadResponse extends UploadRecord {
    merged: {
        framework: string;
        profile_id: string;
        profile_version: number;
        /** framework → `PROVENANCE_SOURCES` value, for every layer. */
        provenance: Record<string, string>;
        /** Layers this merge did not touch. */
        untouched_frameworks: string[];
    };
    guide: {
        unpublished_document_ids: string[];
        purged_document_ids: string[];
        still_ingested_document_ids: string[];
        regeneration: GuideRegenerationState;
        /** Non-null exactly when something was unpublished. Render it. */
        message: string | null;
    };
}

/** `GET /support/uploads/{subject_email}` — support admins only, read-only. A support
 *  admin cannot confirm a merge on somebody's behalf; a merge is consent. */
export interface UploadSupportResponse {
    subject_email: string;
    uploads: UploadRecord[];
    parse_job_module: string;
    optional_notice: string;
}

// ── ST-8: skills ─────────────────────────────────────────────────────────────

/** `skills.SOURCE_*`. `manager` exists in the vocabulary and no endpoint writes
 *  it today. */
export type SkillOrigin = 'declared' | 'inferred' | 'manager';

/** One citation behind an inferred skill — `skill_inference.EvidenceItem`.
 *
 *  PRIVATE TO THE SUBJECT. These are meeting titles and document names
 *  gathered AS the subject, so they have not passed `meeting_participants` or
 *  a document ACL on the way to anybody else's screen.
 *  They appear only on `GET /skills`, the confirm/deny responses, and the
 *  support read. `GET /skills/people/{email}` has no evidence field at
 *  all, by type. */
export interface SkillEvidenceItem {
    kind: string;
    ref: string;
    title: string;
    phrase: string;
    excerpt: string;
    when: string | null;
    basis: string;
    detail?: Record<string, unknown>;
}

/** `skills.private_dict` — a skill as its SUBJECT (or a support admin) sees it. */
export interface PrivateSkill {
    skill_key: string;
    name: string;
    cluster: string | null;
    level: SkillLevel | null;
    origin: SkillOrigin;
    confirmed: boolean;
    confirmed_at: string | null;
    denied: boolean;
    denied_at: string | null;
    /** Why Cubie suggested it. `''` for a self-declared skill. */
    rationale: string;
    evidence: SkillEvidenceItem[];
    evidence_ref: string | null;
}

/** `skills.public_dict` — a CONFIRMED skill as a colleague sees it. No evidence
 *  field exists on this shape, and that is enforced server-side by the DTO
 *  rather than by a serializer choosing to omit it. */
export interface PublicSkill {
    skill_key: string;
    name: string;
    cluster: string | null;
    level: SkillLevel | null;
    confirmed: true;
    confirmed_at: string | null;
    origin: SkillOrigin;
}

/** `_own_payload` — the caller's own skills in the four buckets a person thinks
 *  in. `declared` and `accepted_suggestions` are BOTH confirmed and both
 *  colleague-visible; the split is presentation over `origin`.
 *
 *  Four empty buckets is an ordinary, complete answer. Nothing here is a to-do. */
export interface OwnSkillsResponse {
    user_email: string;
    declared: PrivateSkill[];
    accepted_suggestions: PrivateSkill[];
    suggested: PrivateSkill[];
    denied: PrivateSkill[];
    levels: SkillLevel[];
    advisory_notice: string;
    suggestion_notice: string;
    denial_notice: string;
}

/** `GET /skills/taxonomy`. `seeded` is the difference between "no skill matches
 *  your search" and "the vocabulary has not been built yet" — two different
 *  screens that `[]` alone cannot tell apart. */
export interface SkillTaxonomyResponse {
    skills: { skill_key: string; name: string; cluster: string | null; aliases: string[] }[];
    clusters: string[];
    levels: SkillLevel[];
    seeded: boolean;
    advisory_notice: string;
}

/** `POST /skills/declare` — `DeclareResult.to_dict()` spread over the caller's
 *  refreshed own-skills payload, so one response repaints the whole page.
 *
 *  `undenied` names skills whose earlier denial this declaration lifted. Say so;
 *  reversing a "no" silently is worse than mentioning it. */
export interface DeclareSkillsResponse extends OwnSkillsResponse {
    created: string[];
    updated: string[];
    unchanged: string[];
    undenied: string[];
}

/** `POST /skills/{skill_key}/confirm` and `.../deny` — the one row that changed,
 *  plus the refreshed own-skills payload. `deny` additionally repeats
 *  `denial_notice`, which `OwnSkillsResponse` already carries. */
export interface SkillMutationResponse extends OwnSkillsResponse {
    skill: PrivateSkill;
}

/** `POST /skills/refresh` (202). Scoped to the caller alone; there is no
 *  per-run poll and no progress. Re-read `GET /skills`. */
export interface SkillRefreshResponse {
    status: 'queued';
    user_email: string;
    job_module: string;
    executor: string;
    execution_name: string;
    suggestion_notice: string;
    advisory_notice: string;
}

/** `GET /skills/people/{subject_email}` — a colleague's CONFIRMED skills, for
 *  staffing. Pending suggestions and denials are both excluded by one predicate.
 *  404 for somebody who is not in the company. */
export interface ColleagueSkillsResponse {
    subject_email: string;
    skills: PublicSkill[];
    advisory_notice: string;
    /** Says a short list is not a complete picture of somebody. Render it. */
    scope_notice: string;
}

/** `GET /skills/support/{subject_email}` — support admins only. */
export interface SkillSupportResponse extends OwnSkillsResponse {
    sweep_job_module: string;
}

/** The body of `POST /skills/declare`. `null` is "I do this, at no stated
 *  level". A key outside the ACTIVE taxonomy is a 400 for the WHOLE batch —
 *  nothing is written — and the detail names the path forward. */
export interface DeclareSkillsBody {
    skills: Record<string, SkillLevel | null>;
}

// ── ST-7: the pair working-relationship guide (COPILOT-GATED SURFACE) ────────
//
// The API carries the company-wide gate, but plan.md Decision 4 puts the Compare
// SCREEN behind the copilot gate (`canAccessFullApp`). Gate the nav and the route
// on that flag; the client itself does not check anything.

/** `_document_dict` in `smart_teams_comparison.py`.
 *
 *  `published` and `rag_ingested` are always false and nothing in the feature
 *  writes them — a pair guide is never published and never in the RAG corpus.
 *  They are reported so a client can SEE that rather than take it on trust.
 *
 *  `subject_emails` is ordered `[reader, about]`, which is also the cache
 *  identity: `(A, B)` and `(B, A)` are different documents and this endpoint only
 *  ever returns the one addressed to the caller. */
export interface PairGuideDocument {
    id: string;
    kind: 'pair_guide';
    subject_emails: string[];
    reader_email: string | null;
    about_email: string | null;
    display_names: Record<string, string>;
    title: string;
    status: DocumentStatus;
    error: string | null;
    /** email → the profile version this guide was written from. */
    profile_versions: Record<string, number | null>;
    current_profile_versions: Record<string, number | null>;
    /** Either person retaking makes this true. Server-decided. */
    stale: boolean;
    published: boolean;
    rag_ingested: boolean;
    created_at: string | null;
    updated_at: string | null;
    advisory_notice: string;
    content_md?: string;
}

/** `GET /comparison/pair/{other_email}`. 200 with `state: 'none'` when nobody
 *  has generated one; 403 when the consent gate is closed (either person has not
 *  published their own personal guide) — and that check runs BEFORE the row is
 *  looked at, so a refusal never reveals whether a document exists.
 *
 *  `can_generate` already folds in BOTH the consent gate and both profile
 *  versions being present. Do not re-derive it. */
export interface PairGuideResponse {
    state: 'none' | DocumentStatus;
    guide: PairGuideDocument | null;
    current_profile_versions: Record<string, number | null>;
    display_names: Record<string, string>;
    can_generate: boolean;
    advisory_notice: string;
}

/** `POST /comparison/pair/{other_email}/generate` (202). Same cached/enqueued
 *  discrimination as the personal guide. 403 with the refusal sentence when both
 *  people have not each published their own personal guide first — that gate is
 *  STRICTER than the read gate, and the sentence is written to be shown. */
export type GeneratePairGuideResponse =
    | {
          enqueued: false;
          reason: 'cached';
          guide: PairGuideDocument;
          advisory_notice: string;
      }
    | {
          enqueued: true;
          reason: 'generating';
          executor: string;
          execution_name: string;
          guide: PairGuideDocument;
          advisory_notice: string;
      };

/** `GET /comparison/pair/support/{reader_email}/{about_email}` — support admins only,
 *  and `can_view_raw_profile` must pass for BOTH people. State without prose. */
export interface PairGuideSupportResponse {
    reader_email: string;
    about_email: string;
    display_names: Record<string, string>;
    current_profile_versions: Record<string, number | null>;
    /** email → whether that person has published their own personal guide. This
     *  is the consent map, and it is why a pair guide is or is not readable. */
    published_personal_guides: Record<string, boolean>;
    guides: PairGuideDocument[];
}

// ── ST-9: Team Builder (COPILOT-GATED SURFACE) ───────────────────────────────
//
// This router carries BOTH gates server-side (`require_smart_teams_access` +
// `require_full_app_access`), so gate the nav and the route on `canAccessFullApp`
// or a user without the copilot gets a 403 from a page they were invited to
// open.

/** `team_contracts.AVAILABILITY_STATES`. A state and a `back_on` date — never a
 *  day count, because "out 6 days" is a number that sorts people. */
export type TeamAvailability =
    | 'available'
    | 'out_part_of_window'
    | 'out_for_window'
    | 'unknown';

/** `team_contracts.STYLE_MIX_VALUES`. */
export type TeamStyleMix = 'complementary' | 'aligned' | 'unspecified';

/** `team_contracts.STATE_*` — derived from the shortfalls, never set. */
export type TeamAnswerState = 'complete' | 'partial' | 'empty';

/** `team_contracts.SHORTFALL_CODES`. A closed set, so a page may switch on it. */
export type TeamShortfallCode =
    | 'skill_not_in_taxonomy'
    | 'no_one_with_skill'
    | 'not_enough_with_skill'
    | 'everyone_out'
    | 'no_published_profiles'
    | 'size_not_met'
    | 'window_beyond_horizon';

/** One person in a proposed group. Deliberately unrankable: every field is an
 *  identity, a set, or a state. There is no score, no rank, no match percentage
 *  and no "fit", and `team_contracts._forbid_person_metrics` breaks the backend
 *  at import if one is ever added — so the UI must not compute one either
 *  (no "covers 3 of 4", no percentage bar, no sort by coverage).
 *
 *  `work_style` is the raw DISC style KEY (`"D"`, `"D+I"`, `"BALANCED"`), and the
 *  router does NOT send a human label for it — see the note in the report. Do
 *  not print the key raw; the group-level `style_balance` and `write_up` are
 *  where working style is meant to be read. */
export interface TeamMember {
    email: string;
    name: string;
    confirmed: { skill: string; name: string; level: SkillLevel | null; origin: SkillOrigin }[];
    availability: TeamAvailability;
    back_on: string | null;
    work_style: string | null;
    /** Where `work_style` sits on the team map. `null` = not placed. */
    style_marker: StyleMarker | null;
    clusters: string[];
}

/** `_answer_dict` in `smart_teams_teams.py`. `POST /teams` always 200 when the
 *  ask parses — an ask nobody can satisfy is `state: 'empty'` with `shortfalls`
 *  saying why, NOT an error.
 *
 *  `report` is server-rendered markdown of the whole answer and `write_up` is
 *  the authored/model-composed rationale lines. Both are written server-side and
 *  the UI adds no judgement of its own. */
export interface TeamAnswerResponse {
    state: TeamAnswerState;
    asked_for: { skill: string; name: string; phrase: string; required: boolean }[];
    /** Phrases that did not resolve to a curated skill. Reported, never coerced
     *  onto a neighbouring skill. Show them. */
    unrecognised: string[];
    window: { start: string; end: string } | null;
    group: TeamMember[];
    coverage: {
        skill: string;
        name: string;
        required: boolean;
        /** Emails in roster (alphabetical) order. NOT a preference order, and
         *  there is no primary holder of a skill. */
        covered_by: string[];
        covered: boolean;
    }[];
    /** GROUP facts only. `styles_present` is a sorted multiset — sorted so it
     *  cannot be zipped back onto `group` and read as a per-person list. Do not
     *  attempt that. */
    style_balance: {
        clusters_covered: string[];
        clusters_missing: string[];
        cluster_notes: Record<string, string>;
        styles_present: string[];
        all_one_style: boolean;
        profiles_missing_for: number;
        note: string;
    };
    /** The pair guide's ten dimensions as GROUP counts: `open_for` of
     *  `of_people`. There is deliberately no per-person mapping — that would
     *  publish who has not finished their interview. */
    dimension_readiness: { key: string; label: string; open_for: number; of_people: number }[];
    unreadable_notes: string[];
    shortfalls: {
        code: TeamShortfallCode;
        detail: string;
        skill: string | null;
        phrase: string | null;
    }[];
    notes: string[];
    horizon_note: string | null;
    write_up: string[];
    report: string;
    advisory_notice: string;
    selection_notice: string;
    scope_notice: string;
    availability_notice: string;
}

/** `GET /teams/skills` — the vocabulary a Team Builder ask may use. Skills, never
 *  people. */
export interface TeamSkillVocabularyResponse {
    skills: { skill: string; name: string; cluster: string | null }[];
    clusters: string[];
    max_skills_per_ask: number;
    max_team_size: number;
    style_mix_values: TeamStyleMix[];
    advisory_notice: string;
}

/** The body of `POST /teams` (`TeamAskBody`).
 *
 *  `skills` / `nice_to_have` are FREE-TEXT phrases resolved exactly against the
 *  taxonomy — at least one of the two must be non-empty or it is a 400. Dates are
 *  `YYYY-MM-DD`. There is no `requested_by` and there may not be one: the
 *  requester is the authenticated caller, which is what makes running Team
 *  Builder as somebody else unsayable rather than merely refused. */
export interface TeamAskBody {
    skills?: string[];
    nice_to_have?: string[];
    size?: number | null;
    starts_on?: string | null;
    ends_on?: string | null;
    style_mix?: TeamStyleMix;
    /** Strengths clusters to aim for. An unknown one is a 400. */
    clusters?: string[];
    /** The ask in the person's own words, carried into the write-up prompt. */
    ask?: string;
}

// ── Calls: the caller's own full answers, and the support reads ───────────────

/** The caller's own complete answers blob. Separate from the resume read so the
 *  ordinary poll does not ship 155 answers on every request. Returns
 *  `{ status: 'not_started' }` when there is no row. */
export async function getOwnAnswers(): Promise<
    ResumeState & { answers: Record<string, AnswerValue> } | { status: 'not_started' }
> {
    const { data } = await client.get('/api/smart-teams/interview/answers');
    return data;
}

/** Support: one person's raw interview. `can_view_raw_profile` only —
 *  a 403 here is the expected answer for everybody else, including an exec and
 *  a team manager. Gate any UI on `canViewSmartTeamsRawData`. */
export async function supportReadInterview(
    subjectEmail: string,
): Promise<
    | (ResumeState & {
          answers: Record<string, AnswerValue>;
          subject_email: string;
          profile: ProfileSummary | null;
      })
    | { status: 'not_started'; subject_email: string }
> {
    const { data } = await client.get(
        `/api/smart-teams/support/interviews/${encodeURIComponent(subjectEmail)}`,
    );
    return data;
}

// ── Calls: ST-6 guides ───────────────────────────────────────────────────────

/** The caller's own guide, draft or published. Always 200. */
export async function getOwnGuide(): Promise<OwnGuideResponse> {
    const { data } = await client.get<OwnGuideResponse>('/api/smart-teams/guides/me');
    return data;
}

/** A colleague's PUBLISHED guide. 404 when there is none to read (which also
 *  covers "they have an unpublished draft" — do not try to tell those apart).
 *  Passing one's own address is legal and returns one's own draft. */
export async function getColleagueGuide(subjectEmail: string): Promise<ColleagueGuideResponse> {
    const { data } = await client.get<ColleagueGuideResponse>(
        `/api/smart-teams/guides/${encodeURIComponent(subjectEmail)}`,
    );
    return data;
}

/** Generate or regenerate the CALLER's own guide. 202. There is no subject
 *  parameter and there will not be one. 409 when there is no scored profile yet,
 *  or when the caller's guide for this version is published (unpublish first). */
export async function generateOwnGuide(): Promise<GenerateGuideResponse> {
    const { data } = await client.post<GenerateGuideResponse>(
        '/api/smart-teams/guides/generate',
    );
    return data;
}

/** The subject approves their own guide, which publishes it. Only the subject —
 *  a support admin gets a 403, deliberately: this is consent. */
export async function approveGuide(documentId: string): Promise<ApproveGuideResponse> {
    const { data } = await client.post<ApproveGuideResponse>(
        `/api/smart-teams/guides/${encodeURIComponent(documentId)}/approve`,
    );
    return data;
}

/** The subject retracts their guide. In-app reads stop immediately; the corpus
 *  copy is removed by cubie-worker (`rag_purge`). Idempotent. */
export async function unpublishGuide(documentId: string): Promise<UnpublishGuideResponse> {
    const { data } = await client.post<UnpublishGuideResponse>(
        `/api/smart-teams/guides/${encodeURIComponent(documentId)}/unpublish`,
    );
    return data;
}

/** The branded Word export, as a Blob. Same `can_view_guide` gate as the JSON
 *  read. The caller is responsible for the download — see `guideDocxFilename`. */
export async function downloadGuideDocx(documentId: string): Promise<Blob> {
    const { data } = await client.get<Blob>(
        `/api/smart-teams/guides/${encodeURIComponent(documentId)}/docx`,
        { responseType: 'blob' },
    );
    return data;
}

/** The PDF export, as a Blob — the copy meant to be SENT rather than edited.
 *
 *  Same `can_view_guide` gate and the same stored `content_md` as the Word
 *  export, so the two files always say the same thing. A PDF is not tamper-proof
 *  and the UI should not imply it is; what it carries is the signal that this is
 *  a document to read rather than a template to fork. */
export async function downloadGuidePdf(documentId: string): Promise<Blob> {
    const { data } = await client.get<Blob>(
        `/api/smart-teams/guides/${encodeURIComponent(documentId)}/pdf`,
        { responseType: 'blob' },
    );
    return data;
}

/** Support: every generation for one person, no bodies. */
export async function supportReadGuides(subjectEmail: string): Promise<GuideSupportResponse> {
    const { data } = await client.get<GuideSupportResponse>(
        `/api/smart-teams/guides/support/${encodeURIComponent(subjectEmail)}`,
    );
    return data;
}

// ── Calls: ST-5 uploads ──────────────────────────────────────────────────────

/** The caller's own uploads, newest first. `[]` is the normal answer. */
export async function listOwnUploads(): Promise<UploadListResponse> {
    const { data } = await client.get<UploadListResponse>('/api/smart-teams/uploads');
    return data;
}

/** Store one official report of the caller's own and queue the parse. 202,
 *  multipart — the field name is `file` and the router 400s a file with no name.
 *  PDF only, 10 MB ceiling, both enforced server-side with a sentence to show. */
export async function uploadOfficialReport(file: File): Promise<CreateUploadResponse> {
    const form = new FormData();
    form.append('file', file);
    const { data } = await client.post<CreateUploadResponse>(
        '/api/smart-teams/uploads',
        form,
    );
    return data;
}

/** One of the caller's own uploads, with the proposal once the parse has run.
 *  404 for an id they do not own — the ownership test is inside the query, so
 *  this never confirms that somebody else's id exists. */
export async function getOwnUpload(uploadId: string): Promise<UploadRecord> {
    const { data } = await client.get<UploadRecord>(
        `/api/smart-teams/uploads/${encodeURIComponent(uploadId)}`,
    );
    return data;
}

/** Confirm the mapping; the merge happens synchronously and writes one new
 *  profile version. `framework` is an ECHO of the framework the person SAW
 *  proposed — send it so a proposal that changed underneath them cannot be
 *  confirmed by accident. It never selects which layer the numbers land in.
 *  409 (with the library's sentence) for a state refusal. */
export async function confirmUpload(
    uploadId: string,
    framework?: string | null,
): Promise<ConfirmUploadResponse> {
    const { data } = await client.post<ConfirmUploadResponse>(
        `/api/smart-teams/uploads/${encodeURIComponent(uploadId)}/confirm`,
        { framework: framework ?? null },
    );
    return data;
}

/** The person says no. Nothing about their profile changes; the row and the
 *  stored file are kept. Rejecting twice is a no-op. */
export async function rejectUpload(uploadId: string): Promise<UploadRecord> {
    const { data } = await client.post<UploadRecord>(
        `/api/smart-teams/uploads/${encodeURIComponent(uploadId)}/reject`,
    );
    return data;
}

/** Support: one person's uploads. Read-only — support is not consent. */
export async function supportReadUploads(
    subjectEmail: string,
): Promise<UploadSupportResponse> {
    const { data } = await client.get<UploadSupportResponse>(
        `/api/smart-teams/support/uploads/${encodeURIComponent(subjectEmail)}`,
    );
    return data;
}

// ── Calls: ST-8 skills ───────────────────────────────────────────────────────

/** The caller's own skills — declared, accepted, pending, denied. Evidence
 *  included, because the audience is the subject. */
export async function getOwnSkills(): Promise<OwnSkillsResponse> {
    const { data } = await client.get<OwnSkillsResponse>('/api/smart-teams/skills');
    return data;
}

/** The curated vocabulary for the picker. Matched against name AND aliases, so
 *  "hubspot" finds "Pipeline and CRM hygiene". Check `seeded` before rendering
 *  an empty list as "no matches". */
export async function searchSkillTaxonomy(opts?: {
    q?: string;
    cluster?: string;
    limit?: number;
}): Promise<SkillTaxonomyResponse> {
    const { data } = await client.get<SkillTaxonomyResponse>(
        '/api/smart-teams/skills/taxonomy',
        { params: { q: opts?.q || undefined, cluster: opts?.cluster || undefined, limit: opts?.limit } },
    );
    return data;
}

/** The caller states skills about THEMSELVES. `confirmed` by definition.
 *  400 (whole batch rejected, nothing written) for a key outside the ACTIVE
 *  taxonomy or a level that is not a level — show the detail verbatim, it names
 *  the path forward. */
export async function declareOwnSkills(
    body: DeclareSkillsBody,
): Promise<DeclareSkillsResponse> {
    const { data } = await client.post<DeclareSkillsResponse>(
        '/api/smart-teams/skills/declare',
        body,
    );
    return data;
}

/** Yes to a suggestion about oneself. Idempotent. 404 when there is no
 *  suggestion for that skill — a confirm never creates a row; `declare` does. */
export async function confirmOwnSkill(skillKey: string): Promise<SkillMutationResponse> {
    const { data } = await client.post<SkillMutationResponse>(
        `/api/smart-teams/skills/${encodeURIComponent(skillKey)}/confirm`,
    );
    return data;
}

/** No, and the refusal STICKS across every later sweep. Also the retraction path
 *  for a skill the caller declared. Denying twice is a no-op. */
export async function denyOwnSkill(skillKey: string): Promise<SkillMutationResponse> {
    const { data } = await client.post<SkillMutationResponse>(
        `/api/smart-teams/skills/${encodeURIComponent(skillKey)}/deny`,
    );
    return data;
}

/** Ask for a fresh look at the caller's OWN internal activity. 202, one person,
 *  no progress to poll. Safe to press twice: an unchanged evidence corpus costs
 *  nothing, and a denied skill cannot come back. */
export async function refreshOwnSuggestions(): Promise<SkillRefreshResponse> {
    const { data } = await client.post<SkillRefreshResponse>(
        '/api/smart-teams/skills/refresh',
    );
    return data;
}

/** A colleague's CONFIRMED skills. 404 for a non-employee. */
export async function getColleagueSkills(
    subjectEmail: string,
): Promise<ColleagueSkillsResponse> {
    const { data } = await client.get<ColleagueSkillsResponse>(
        `/api/smart-teams/skills/people/${encodeURIComponent(subjectEmail)}`,
    );
    return data;
}

/** Support: one person's full skills, evidence included. */
export async function supportReadSkills(
    subjectEmail: string,
): Promise<SkillSupportResponse> {
    const { data } = await client.get<SkillSupportResponse>(
        `/api/smart-teams/skills/support/${encodeURIComponent(subjectEmail)}`,
    );
    return data;
}

/** One person who confirmed one skill, as a colleague sees them.
 *  `skills.SkillHolder.public_dict` — primitives only, and deliberately NO
 *  evidence: this read fans out over the roster, so a serializer slip here would
 *  leak an ACL'd meeting title about everybody at once. */
export interface SkillHolder {
    email: string;
    display_name: string;
    level: SkillLevel | null;
    /** `declared` (they told us) or `inferred` (Cubie suggested, they agreed).
     *  Worth showing; not evidence, and not a ranking. */
    origin: SkillOrigin;
    confirmed_at: string | null;
}

/** One asked-for term, what it resolved to, and who holds it. */
export interface SkillHolderMatch {
    /** Echoed verbatim so a UI can line results up against what was typed. */
    term: string;
    skill_key: string;
    name: string;
    /** Everyone who confirmed it, in NAME order. Not ranked — see the route. */
    holders: SkillHolder[];
}

/** `GET /skills/holders`. `unknown` is load-bearing: "nobody has this skill" and
 *  "we do not track this skill" are different answers, and a UI that shows an
 *  empty list for both tells the reader the wrong one. */
export interface SkillHoldersResponse {
    asked: string[];
    matched: SkillHolderMatch[];
    unknown: string[];
    advisory_notice: string;
    scope_notice: string;
}

/** One row of the curation screen. `managed_in_code` is why the UI must not
 *  offer to edit some rows: the seeder overwrites name/cluster/aliases from the
 *  release on every run, so an edit to a shipped skill would appear to work and
 *  then silently revert. `claims` is why a row can never be deleted. */
export interface CuratedSkillRow {
    skill_key: string;
    name: string;
    cluster: string | null;
    aliases: string[];
    is_active: boolean;
    managed_in_code: boolean;
    claims: number;
}

/** `GET /skills/admin/taxonomy` — every skill, ACTIVE OR RETIRED. Retired rows
 *  are included on purpose: without them an admin cannot understand the 409 they
 *  get for a key that already exists in a retired state. */
export interface CuratedTaxonomyResponse {
    clusters: string[];
    skills: CuratedSkillRow[];
    curation_notice: string;
    advisory_notice: string;
}

export interface NewCuratedSkill {
    skill_key: string;
    name: string;
    cluster: string;
    aliases: string[];
}

/** A PATCH: every field absent means unchanged. `is_active: false` is
 *  retirement, which is the only removal there is. */
export interface CuratedSkillEdit {
    name?: string;
    cluster?: string;
    aliases?: string[];
    is_active?: boolean;
}

// ── Calls: ST-8 skills → people, and curation ────────────────────────────────

/** Skills → PEOPLE. The inverse of `getColleagueSkills`, and the direction a
 *  project lead actually asks in.
 *
 *  `terms` may be skill keys or everyday phrasing — "hubspot" finds CRM hygiene.
 *  400 for an empty list or more than `MAX_HOLDER_TERMS` (6): each term is a
 *  fan-out over the roster, and somebody who needs eight distinct competences is
 *  describing a team, which Team Builder answers better. */
export async function findSkillHolders(terms: string[]): Promise<SkillHoldersResponse> {
    const { data } = await client.get<SkillHoldersResponse>(
        '/api/smart-teams/skills/holders',
        { params: { skills: terms.join(',') } },
    );
    return data;
}

/** Support admins only (403 otherwise, including for execs and Team Managers). */
export async function listCuratedSkills(): Promise<CuratedTaxonomyResponse> {
    const { data } = await client.get<CuratedTaxonomyResponse>(
        '/api/smart-teams/skills/admin/taxonomy',
    );
    return data;
}

/** Add one skill to the company vocabulary. 201 on success.
 *
 *  400 carries `detail.problems`, a LIST of every problem found rather than the
 *  first — render all of them, that is the point. 409 means the key exists, and
 *  its message says whether the existing row is retired and should be
 *  reactivated instead. */
export async function createCuratedSkill(
    body: NewCuratedSkill,
): Promise<{ skill: CuratedSkillRow; notice: string }> {
    const { data } = await client.post<{ skill: CuratedSkillRow; notice: string }>(
        '/api/smart-teams/skills/admin/taxonomy',
        body,
    );
    return data;
}

/** Rename, re-cluster, re-alias or retire ONE admin-added skill.
 *  409 for a skill that ships with the app — the seeder would overwrite it. */
export async function editCuratedSkill(
    skillKey: string,
    body: CuratedSkillEdit,
): Promise<{ skill: CuratedSkillRow }> {
    const { data } = await client.patch<{ skill: CuratedSkillRow }>(
        `/api/smart-teams/skills/admin/taxonomy/${encodeURIComponent(skillKey)}`,
        body,
    );
    return data;
}

// ── Calls: ST-7 pair guide ───────────────────────────────────────────────────

/** The pair guide written TO the caller ABOUT `otherEmail`. Always 200 when the
 *  pair is readable; 403 when either person has not published their own guide;
 *  404 for a non-employee or for the caller's own address. */
export async function getPairGuide(otherEmail: string): Promise<PairGuideResponse> {
    const { data } = await client.get<PairGuideResponse>(
        `/api/smart-teams/comparison/pair/${encodeURIComponent(otherEmail)}`,
    );
    return data;
}

/** Generate or regenerate that guide. 202. 403 with the refusal sentence unless
 *  BOTH people have published their own personal guide. */
export async function generatePairGuide(
    otherEmail: string,
): Promise<GeneratePairGuideResponse> {
    const { data } = await client.post<GeneratePairGuideResponse>(
        `/api/smart-teams/comparison/pair/${encodeURIComponent(otherEmail)}/generate`,
    );
    return data;
}

/** Support: the rows for one ORDERED pair, state without prose.
 *  `can_view_raw_profile` must pass for BOTH people. */
export async function supportReadPairGuides(
    readerEmail: string,
    aboutEmail: string,
): Promise<PairGuideSupportResponse> {
    const { data } = await client.get<PairGuideSupportResponse>(
        `/api/smart-teams/comparison/pair/support/${encodeURIComponent(readerEmail)}/${encodeURIComponent(aboutEmail)}`,
    );
    return data;
}

// ── Calls: ST-9 Team Builder ─────────────────────────────────────────────────

/** Propose a group for a piece of work. Synchronous. 200 whenever the ask
 *  parses — `state: 'empty'` plus `shortfalls` is the truthful answer to an ask
 *  nobody satisfies, not an error. 400 for an empty ask, too many skills, a
 *  window that ends before it starts, an out-of-range size, or an unknown
 *  cluster; the detail is the contract's own sentence.
 *
 *  COPILOT-GATED server-side (`require_full_app_access`). */
export async function buildTeam(body: TeamAskBody): Promise<TeamAnswerResponse> {
    const { data } = await client.post<TeamAnswerResponse>('/api/smart-teams/teams', body);
    return data;
}

/** The skill names a Team Builder ask may use. Skills, never people. Copilot-gated
 *  server-side, same as `buildTeam`. */
export async function getTeamSkillVocabulary(opts?: {
    query?: string;
    cluster?: string;
    limit?: number;
}): Promise<TeamSkillVocabularyResponse> {
    const { data } = await client.get<TeamSkillVocabularyResponse>(
        '/api/smart-teams/teams/skills',
        { params: { query: opts?.query || undefined, cluster: opts?.cluster || undefined, limit: opts?.limit } },
    );
    return data;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** The filename to save a `downloadGuideDocx` blob under.
 *
 *  The router sends the real one in `Content-Disposition`; the browser does not
 *  expose that header to XHR unless it is allowlisted, and it is not, so this
 *  reproduces `guide.export_docx`'s own name instead of reading a header that
 *  may be absent. Transcribed from `guide._slug` — lowercase, every run of
 *  non-alphanumerics to a single hyphen, trimmed, and `guide` when nothing is
 *  left — so the file a person saves matches the one the server named.
 *  Cosmetic only: the bytes are the export either way. */
export function guideDocxFilename(displayName: string): string {
    return `${guideFileStem(displayName)}.docx`;
}

/** The filename to save a `downloadGuidePdf` blob under. Same stem, same
 *  reason — see `guideDocxFilename`. */
export function guidePdfFilename(displayName: string): string {
    return `${guideFileStem(displayName)}.pdf`;
}

/** `guide._slug` transcribed once, so the two export filenames cannot drift. */
function guideFileStem(displayName: string): string {
    const slug =
        (displayName || '')
            .trim()
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '') || 'guide';
    return `how-to-work-with-${slug}`;
}

// ── The overlap heatmap ──────────────────────────────────────────────────────
//
// Three surfaces, two endpoints, one vocabulary. `rubik_core/smart_teams/
// heatmap.py` is the authority for every rule below; the notes here say what a
// CLIENT must not get wrong.
//
// 1. `score` is OPTIONAL AND USUALLY ABSENT. It is serialized only when
//    `can_view_raw_profile` passed for that cell's subject — you for your own
//    cells, a support admin for anyone's. The key is omitted rather than nulled,
//    so `'score' in cell` is the test and `cell.score ?? 0` is a bug: it would
//    render a withheld number as a real zero.
// 2. `not_comparable` IS NOT `aligned`. A row where either person has nothing
//    reportable must be rendered differently from a row where both landed in the
//    same band. Same-band means you match; not-comparable means we could not
//    look, and greying both the same way reports a match nobody measured.
// 3. Motivators appear on the OWN grid and never on a pair grid, because
//    `emphasis` is within-person with no norm group. `cross_person: false` marks
//    it; do not build a two-person view from a cell carrying that flag.

/** One person's dot on the team map — `heatmap.style_marker`.
 *
 *  `x` / `y` are in the unit circle on the instrument's own two axes: x is
 *  communion (getting along) and y is agency (getting ahead), so D sits
 *  up-left, I up-right, S down-right and C down-left. That arrangement falls out
 *  of `disc.py`'s axis sentence, not out of any vendor's chart.
 *
 *  The position averages DISC and Big Five — both read those same two axes.
 *  Motivators (within-person, no norm group), Enneagram (a type, not a scale)
 *  and strengths (clusters, not a direction) never contribute, and `type16` is
 *  derived from Big Five so counting it would weight that layer twice.
 *
 *  EVERY placed dot is at the same radius. The map says which way somebody
 *  leans and deliberately never how much — a radius that grew with intensity
 *  would be the withheld number drawn as a picture. `(0, 0)` is the All-Rounder,
 *  which is a real finding; a person with no marker at all is "not placed", and
 *  must still be listed rather than dropped from the picture.
 *
 *  Write DISC in FULL CAPS wherever this is rendered. The lowercase-i styling is
 *  Wiley's trademark and `disc.py` forbids it outright in this feature's output. */
export interface StyleMarker {
    /** `null` when DISC is closed but Big Five still placed them. */
    style_key: string | null;
    /** Our own style name — "Front Runner", not a vendor's vocabulary. */
    style_name: string | null;
    primary: string | null;
    secondary: string | null;
    /** Which layers actually voted — `disc`, `big_five`, or both. Say so on
     *  screen: a dot from one open layer is less settled than one from two. */
    placed_by: string[];
    /** Strengths clusters this person covers, in `heatmap.CLUSTER_ORDER`. Drawn
     *  as the badge ring around the dot — each cluster owns a FIXED slot, so it
     *  reads by position and not by colour alone. An empty array means the
     *  strengths layer is closed and NO ring is drawn: four empty slots would be
     *  a claim about somebody that nobody measured. */
    clusters: string[];
    x: number;
    y: number;
}

/** `heatmap.OVERLAP_*`. Four states, and the fourth is not a shade of the first. */
export type OverlapState = 'aligned' | 'near' | 'apart' | 'not_comparable';

/** One person's standing on one dimension.
 *
 *  `band_ordinal` is the position on that framework's own ladder and
 *  `band_count` its length — DISC has four rungs and Big Five five, so a colour
 *  ramp must scale by `band_count` rather than assume a shared maximum.
 *  `band_ordinal` is null for a band this release does not recognise (a profile
 *  scored by an older instrument), which renders as unreadable, not as zero. */
export interface HeatmapCell {
    framework: ProfileFramework;
    key: string;
    name: string;
    band: string | null;
    band_ordinal: number | null;
    band_count: number;
    /** False for a within-person scale (motivators). Never put it on a pair grid. */
    cross_person: boolean;
    /** ABSENT unless the raw-profile gate passed for this cell's subject. */
    score?: number;
}

/** `GET /heatmap/me`. Always 200 — "you have not been scored" is a state.
 *
 *  `shows_numbers` reports whether the gate opened, so the page can say WHY
 *  there are no numbers instead of leaving a reader wondering whether scoring
 *  half-failed. */
export interface OwnHeatmapResponse {
    state: 'not_scored' | 'ready' | 'nothing_reportable';
    cells: HeatmapCell[];
    profile_version: number | null;
    shows_numbers: boolean;
    /** Their own dot. `null` when the DISC layer is not reportable. */
    style_marker: StyleMarker | null;
    advisory_notice: string;
    privacy_notice: string;
}

/** One dimension across a pair. Either side is null when the joint gate is shut,
 *  and `overlap` is then `not_comparable` — both facts, not one inferred from
 *  the other. */
export interface PairHeatmapRow {
    framework: ProfileFramework;
    key: string;
    name: string;
    reader: HeatmapCell | null;
    about: HeatmapCell | null;
    overlap: OverlapState;
}

/** `GET /comparison/pair/{other_email}/heatmap`. 403 unless BOTH people have
 *  published their own guide — stricter than the pair-guide READ, which exempts
 *  the reader's own publish state. See the endpoint docstring for why a live
 *  grid does not get that exemption. Needs no generated pair guide. */
export interface PairHeatmapResponse {
    reader_email: string;
    about_email: string;
    display_names: Record<string, string>;
    rows: PairHeatmapRow[];
    current_profile_versions: Record<string, number | null>;
    /** False when either person has no scored profile at all — which is a
     *  different sentence from "no overlap found". */
    both_scored: boolean;
    /** email → dot, for the two-person map. A value may be `null`. */
    markers: Record<string, StyleMarker | null>;
    advisory_notice: string;
}

/** The caller's own grid, with their own numbers. No subject parameter. */
export async function getOwnHeatmap(): Promise<OwnHeatmapResponse> {
    const { data } = await client.get<OwnHeatmapResponse>('/api/smart-teams/heatmap/me');
    return data;
}

/** Where the caller and one colleague overlap and drift. Bands for the
 *  colleague, always. */
export async function getPairHeatmap(otherEmail: string): Promise<PairHeatmapResponse> {
    const { data } = await client.get<PairHeatmapResponse>(
        `/api/smart-teams/comparison/pair/${encodeURIComponent(otherEmail)}/heatmap`,
    );
    return data;
}
