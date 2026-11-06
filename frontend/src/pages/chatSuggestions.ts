// Starter prompts for the empty Chat screen — the catalogue and the picker.
//
// Why this is a module and not a `const` in Chat.tsx:
//
// 1. **Four fixed prompts taught four things.** The empty state is the only
//    place most people ever learn what Chat can do, and it was advertising
//    email, CRM and meetings — nothing about the knowledge base, Slack,
//    scheduled tasks, who's out of office, or asking Cubie about Cubie.
//    Whole tool surfaces went undiscovered because the front door never
//    mentioned them. The catalogue below covers every group of tools the chat
//    coordinator actually registers (rubik_core/agent_runtime/coordinator.py),
//    capped at MAX_PROMPTS_PER_GROUP per group so no one area can crowd out
//    the rest.
// 2. **A different handful every time.** `pickSuggestedPrompts` shuffles, so a
//    daily user meets new capabilities over a week instead of the same four
//    forever. It spreads across groups before it takes a second prompt from
//    any one of them — six cards should be six different parts of Cubie, not
//    six ways to ask about email.
// 3. **A prompt for a feature you can't reach is worse than no prompt.** A
//    group may name a `requires` key from the rubik_core/features registry;
//    the picker drops it unless the caller's `canUse` says yes. Fail-closed —
//    an unknown verdict hides the group (see `hasFeature` in AuthContext).
// 4. It is testable. Chat.tsx is a 1,500-line component with no test harness;
//    this is a leaf module with type-only imports, so chatSuggestions.test.ts
//    can pin the invariants under `node --test`.
//
// Every prompt here must be something the live coordinator can actually
// answer, and must not invent a document, product, client or person by name —
// a starter prompt that returns "I couldn't find that" teaches the opposite of
// what it is for. Phrasing is copied from the Help page's live examples where
// one exists, so the two homes agree.
import type { FeatureKey } from '../context/AuthContext';

/** Icons live in the component (lucide-react is a runtime import and this
 *  module must stay dependency-free for the test runner), so a group names its
 *  icon and Chat.tsx maps the name to a component. Adding a name here without
 *  adding it to that map is a type error. */
export type SuggestionIconName =
    | 'briefing'
    | 'email'
    | 'calendar'
    | 'meetings'
    | 'crm'
    | 'knowledge'
    | 'products'
    | 'slack'
    | 'people'
    | 'web'
    | 'help'
    | 'schedule'
    | 'teams';

/** The feature-registry keys a suggestion group may gate on — a subset of
 *  AuthContext's `FeatureKey`, so a typo cannot compile and a renamed feature
 *  breaks the build rather than silently showing the group to everybody. */
export type SuggestionGate = Extract<
    FeatureKey,
    'smart_teams' | 'scheduled_tasks'
>;

export interface SuggestionGroup {
    /** Stable id — the React key, and what the tests assert against. */
    id: string;
    icon: SuggestionIconName;
    /** Feature key that must resolve true. Absent = every Chat user (Chat
     *  itself is already behind the `copilot` gate, so no group repeats it). */
    requires?: SuggestionGate;
    /** 1..MAX_PROMPTS_PER_GROUP complete, ready-to-send questions. */
    prompts: string[];
}

/** No feature may offer more than this many starters — the cap that keeps the
 *  catalogue balanced as it grows. Pinned by the test. */
export const MAX_PROMPTS_PER_GROUP = 5;

/** How many cards the empty state shows. Six fills three rows of the two-column
 *  grid: enough breadth to be a tour, short enough that the input box stays
 *  above the fold. */
export const VISIBLE_PROMPT_COUNT = 6;

export const SUGGESTION_CATALOG: SuggestionGroup[] = [
    {
        id: 'briefing',
        icon: 'briefing',
        prompts: [
            'What should I focus on today?',
            'Give me my daily briefing.',
            'Catch me up on what I missed yesterday.',
            'What am I waiting on?',
            'What deadlines are coming up for me?',
        ],
    },
    {
        id: 'email',
        icon: 'email',
        prompts: [
            'Catch me up on my unread emails.',
            'Which emails still need a reply from me?',
            'Summarize the most recent email thread I was added to.',
            'Draft a reply to my most recent unread email.',
            'Draft an email pitching our services to a new prospect.',
        ],
    },
    {
        id: 'calendar',
        icon: 'calendar',
        prompts: [
            "What's on my calendar today?",
            'Find me a free hour this week for focused work.',
            'Do I have any conflicts on my calendar this week?',
            'How much of my week is already booked in meetings?',
            'Set up a 30-minute check-in with my manager next week.',
        ],
    },
    {
        id: 'meetings',
        icon: 'meetings',
        prompts: [
            'Summarize the key takeaways from my last meeting.',
            'What action items came out of my meetings this week?',
            'Give me the full notes from my most recent L10.',
            'Who owns the follow-ups from my last team meeting?',
            'What did we decide the last time we discussed pricing?',
        ],
    },
    {
        id: 'crm',
        icon: 'crm',
        prompts: [
            'Which of my deals are closing this month?',
            "What's our total open pipeline worth?",
            'Which of my open deals are going cold?',
            'How many deals did we close this quarter?',
            'When did we last reach out to my biggest open deal?',
        ],
    },
    {
        id: 'knowledge',
        icon: 'knowledge',
        prompts: [
            'What do our documents say about the client onboarding process?',
            'What is our policy on handling confidential client data?',
            'Summarize what the knowledge base has on our quality review process.',
            'Which documents mention our pricing policy?',
            'Find the most recent version of our services agreement.',
        ],
    },
    {
        id: 'products',
        icon: 'products',
        prompts: [
            'What services do we offer?',
            'Which of our products fits a new client that needs a quick turnaround?',
            'Explain what we deliver on a typical engagement.',
            'What do we charge for our most common service?',
            'How long does a typical engagement take from kickoff to delivery?',
        ],
    },
    {
        id: 'slack',
        icon: 'slack',
        prompts: [
            'What did I miss on Slack today?',
            'Show me my Slack mentions from this week.',
            'Summarize what my team channel has been discussing this week.',
            'Is there anything in Slack I need to act on?',
            'Which Slack channels have unread messages I should read?',
        ],
    },
    {
        id: 'people',
        icon: 'people',
        prompts: [
            'Who is out of office this week?',
            'Is anyone on my team away next week?',
            'Find the contact details for someone on the Marketing team.',
            'Who should I ask about a question on our contracts?',
            "Is anyone I'm meeting with this week out of office?",
        ],
    },
    {
        id: 'web',
        icon: 'web',
        prompts: [
            'What is happening in our industry news this week?',
            'Look up the latest news about our biggest competitor.',
            'What are current interest rate trends?',
            'Look up recent regulation changes that affect small businesses.',
            'What are the main trends in AI tools for sales teams this year?',
        ],
    },
    {
        id: 'cubie-how-to',
        icon: 'help',
        prompts: [
            'How do I add a document to the knowledge base?',
            'What can Chat answer about?',
            'How do I connect Slack to Cubie?',
            'What happens when I mark a priority Done?',
            'Where do I change my working hours in Cubie?',
        ],
    },
    {
        id: 'scheduled-tasks',
        icon: 'schedule',
        requires: 'scheduled_tasks',
        prompts: [
            'Every weekday at 8am, send me my important emails.',
            "Each Monday, summarize last week's meetings for me.",
            'What have I scheduled?',
            'On the 1st of each month, remind me to review the pipeline.',
            'Every Friday, give me a recap of my week.',
        ],
    },
    {
        id: 'smart-teams',
        icon: 'teams',
        requires: 'smart_teams',
        prompts: [
            'Put together a team for a customer onboarding project.',
            'Who should I pull in for a project that needs data analysis experience?',
            'What is the best way to work with someone on the Sales team?',
            'Suggest a small team for a two-week process improvement project.',
            'How should I approach a first working session with a new colleague?',
        ],
    },
];

// ─── The sales daily rhythm ──────────────────────────────────────────────────
//
// Sales asked for their "Daily Operational AI Prompt Checklist" to live in
// Cubie: eleven prompts in three routines. These are those prompts, phrased so
// the live coordinator answers each one — the HubSpot ones run the checks in
// rubik_core/sales/rhythm_checks.py, the rest use Gmail, Slack, My To-Dos and
// the calendar. They are NOT part of the shuffled catalogue above: a rep's
// routine should read the same every day, so Chat shows the current part of
// the day as its own row (see Chat.tsx) and My Day's Daily rhythm section links
// its "+N more" counts to the same questions.
//
// The rep's HubSpot prompts are the SERVER's `ask_prompt` strings verbatim
// (backend/routers/sales_rhythm.py); src/api/salesRhythm.test.ts fails if the
// two drift, because a card's "+4 more" must open the question that lists
// exactly those four.

export type RhythmPart = 'morning' | 'midday' | 'eod';
export type RhythmAudience = 'rep' | 'team';

export const RHYTHM_PARTS: readonly RhythmPart[] = ['morning', 'midday', 'eod'];

export const RHYTHM_PART_LABELS: Record<RhythmPart, string> = {
    morning: 'Morning',
    midday: 'Mid-day',
    eod: 'End of day',
};

/** The checklist's own goal line for each routine. */
export const RHYTHM_PART_GOALS: Record<RhythmPart, string> = {
    morning: 'Catch missed messages, prep for meetings, focus on the deals that matter.',
    midday: 'Chase what is pending, clear pipeline risk, mine old opportunities.',
    eod: 'Tidy the CRM, clear what is overdue, and send the day’s recap.',
};

/** Which routine it is on the reader's own clock: before 11:00 is the morning,
 *  before 15:00 mid-day, after that the end of the day. */
export function rhythmPartFor(now: Date): RhythmPart {
    const h = now.getHours();
    if (h < 11) return 'morning';
    if (h < 15) return 'midday';
    return 'eod';
}

/** The team version wins: a leader on the team roster gets their team's
 *  prompts, which is the version that works for someone who owns no deals. */
export function rhythmAudienceFor(canUse: { rep: boolean; team: boolean }): RhythmAudience | null {
    if (canUse.team) return 'team';
    if (canUse.rep) return 'rep';
    return null;
}

export const SALES_RHYTHM_PROMPTS: Record<RhythmAudience, Record<RhythmPart, string[]>> = {
    rep: {
        morning: [
            "Which prospects or customers emailed or Slacked me since yesterday that I haven't replied to yet?",
            "Which follow-ups, quotes or proposals did I promise to send this week that I haven't done yet?",
            "Prep me for today's external meetings: the last 3 touchpoints, open questions, deal stage and key stakeholders for each.",
            'Which 3 deals closing this month need action from me today to keep them from slipping?',
        ],
        midday: [
            'Which of my deals in a proposal or negotiation stage have been waiting on the decision-maker for more than 3 business days?',
            'Show me my active deals whose close date has been pushed more than twice, or with no activity logged in the last 7 days.',
            'List my active deals over $30k that only have one contact.',
            'Find 5 deals I lost 12–24 months ago over timing or budget where the main contact is still at the company.',
        ],
        eod: [
            'Which contacts or deals I created or updated today are missing a job title, buying role or next step?',
            'Show me my overdue HubSpot tasks and any of my deals whose close date has already passed.',
            'Summarize my calls, emails, meetings and stage changes from today as a short bulleted update for my manager.',
        ],
    },
    // The same routine read across the team, for a leader. Inbox, promises and
    // meeting prep stay personal (nobody reads a colleague's inbox), and the
    // end of the day leaves out colleagues' overdue tasks and activity log: a
    // past-due close date is a view of the pipeline, a colleague's to-do list
    // is a view of the person.
    team: {
        morning: [
            "Which prospects or customers emailed or Slacked me since yesterday that I haven't replied to yet?",
            "Prep me for today's external meetings: the last 3 touchpoints, open questions, deal stage and key stakeholders for each.",
            'Which 10 deals closing this month across my team need action today to keep them from slipping?',
        ],
        midday: [
            'Which deals across my team in a proposal or negotiation stage have been waiting on the decision-maker for more than 3 business days?',
            "Show me my team's active deals whose close date has been pushed more than twice, or with no activity logged in the last 7 days.",
            "List my team's active deals over $30k that only have one contact.",
            'Find 10 deals my team lost 12–24 months ago over timing or budget where the main contact is still at the company.',
        ],
        eod: [
            'Which contacts or deals did my team create or update today that are missing a job title, buying role or next step?',
            "Which of my team's open deals have a close date that has already passed?",
        ],
    },
};

/** One card on the empty screen. */
export interface SuggestedPrompt {
    /** Which catalogue group it came from — the React key, with `text`. */
    groupId: string;
    icon: SuggestionIconName;
    text: string;
}

export interface PickOptions {
    /** How many cards to return. Fewer come back only when the catalogue (after
     *  gating) genuinely holds fewer prompts. */
    count?: number;
    /** The caller's feature verdicts, straight from `/api/users/me` via
     *  AuthContext's `hasFeature`. Omitted = only ungated groups, which is the
     *  right answer while the profile is still loading. */
    canUse?: (key: SuggestionGate) => boolean;
    /** Injected for the tests; defaults to Math.random. */
    rng?: () => number;
    catalog?: SuggestionGroup[];
}

/**
 * A tiny deterministic PRNG (xorshift32), so a draw is a pure function of its
 * seed. The empty state re-renders for all sorts of reasons — a feature
 * verdict landing, a sibling state change, StrictMode's double invoke — and
 * with a bare Math.random every one of those would deal a new hand under the
 * reader's cursor, sometimes mid-click. Holding the seed in state and
 * re-drawing only when it changes means the cards move exactly when the
 * reader asks them to.
 *
 * Adjacent seeds diverge completely, so `seed + 1` is a genuinely different
 * draw rather than a small perturbation of the last one.
 */
export function seededRng(seed: number): () => number {
    // 0 is xorshift's fixed point — it would return 0 forever, i.e. no shuffle
    // at all. Any non-zero substitute will do.
    let state = (Math.abs(Math.trunc(seed)) % 0xffff_ffff) || 0x9e37_79b9;
    return () => {
        state ^= state << 13;
        state ^= state >>> 17;
        state ^= state << 5;
        state >>>= 0;
        return state / 0x1_0000_0000;
    };
}

/** Fisher-Yates on a copy — never shuffles the caller's array in place, which
 *  would quietly reorder the module-level catalogue. */
function shuffled<T>(items: readonly T[], rng: () => number): T[] {
    const out = items.slice();
    for (let i = out.length - 1; i > 0; i--) {
        const j = Math.floor(rng() * (i + 1));
        [out[i], out[j]] = [out[j], out[i]];
    }
    return out;
}

/**
 * Pick a random spread of starter prompts the caller can actually use.
 *
 * Breadth first: the groups are shuffled and visited round-robin, so with six
 * cards and six eligible groups you get six different parts of Cubie. Only
 * once every group has contributed does a second prompt come from any of them,
 * which is what keeps a user with two features from seeing six variations of
 * the same question.
 */
export function pickSuggestedPrompts(options: PickOptions = {}): SuggestedPrompt[] {
    const {
        count = VISIBLE_PROMPT_COUNT,
        canUse = () => false,
        rng = Math.random,
        catalog = SUGGESTION_CATALOG,
    } = options;

    if (count <= 0) return [];

    const eligible = catalog.filter(g => !g.requires || canUse(g.requires));
    // Shuffle the groups AND each group's prompts once, up front: re-drawing
    // per round could hand back the same prompt twice.
    const pools = shuffled(eligible, rng).map(g => ({
        group: g,
        prompts: shuffled(g.prompts, rng),
    }));

    const picked: SuggestedPrompt[] = [];
    for (let round = 0; picked.length < count; round++) {
        let addedThisRound = 0;
        for (const { group, prompts } of pools) {
            if (picked.length >= count) break;
            const text = prompts[round];
            if (text === undefined) continue; // this group is used up
            picked.push({ groupId: group.id, icon: group.icon, text });
            addedThisRound++;
        }
        if (addedThisRound === 0) break; // every group exhausted
    }
    return picked;
}
