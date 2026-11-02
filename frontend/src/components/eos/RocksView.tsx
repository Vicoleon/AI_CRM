import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
    Mountain,
    CheckCircle2,
    Clock,
    AlertTriangle,
    Plus,
    Edit2,
    Trash2,
    Sparkles,
    History,
    Calendar,
    CalendarArrowUp,
    ChevronRight,
} from 'lucide-react';
import client from '../../api/client';
import { errorMessage } from '../../utils/errors';
import {
    Donut,
    EmptyState,
    ErrorState,
    EosPageHeader,
    KpiCard,
    KpiStrip,
    LoadingState,
    OwnerChip,
    PageContainer,
} from '../../pages/eos/_shared';
import {
    formatPerson,
    computeMilestoneProgress,
    formatISODate,
    isMilestoneOverdue,
    nextUpcomingMilestone,
    todayISO,
    EXEC_SCOPE,
    EXEC_SCOPE_LABEL,
    deptSlug,
    matchesTeamFilter,
    preferDeptLabel,
    useTeamFilter,
} from '../../pages/eos/_utils';
import type { PersonLite, TeamScope } from '../../pages/eos/_utils';
import { ClampedText } from '../ClampedText';
import { Modal, Field, inputCls, SubmitButton } from './Modal';
import { RockDetailModal } from './RockDetailModal';
import { toast } from './Toast';
import {
    carryOverMilestones,
    clearDueDatesBefore,
    compareQuarters,
    defaultCarryTarget,
    quarterLabel,
    quarterOfISO,
    quarterStartISO,
} from './rockCarryOver';
import { useAuth } from '../../context/AuthContext';
import type { MeetingContext } from './eosViewTypes';
import type { Issue } from './IssuesView';

/**
 * EOS Implementer prompt template for drafting Rocks. The user's rough goal
 * (the Description field) is appended at the end so Cubie can return
 * a SMART Rock + 3–5 milestones table. Surfaced via the "Draft with AI"
 * button on the Rocks modal; the prompt rides along as ``?q=…`` to /chat.
 */
const ROCK_AI_PROMPT = `Act as an EOS Implementer. Help me write my quarterly Rocks and Milestones using a disciplined, S.M.A.R.T. approach. I will provide a rough goal, and you will return a structured table featuring a clearly defined "Noun-Verb-Date" Rock and 3–5 specific milestones required to reach "Done-Done" status. Keep the tone professional and focused on accountability. After you provide the draft, ask me clarifying questions to ensure the scope is realistic for a 90-day cycle and suggest one way to make the Rock more measurable.`;

export type RockStatus = 'on_track' | 'off_track' | 'done';
export type Visibility = 'company' | 'department' | 'individual';

export interface RockMilestone {
    // Stable server-assigned UUID for a persisted milestone row (absent on a
    // brand-new, not-yet-saved milestone). The whole-array PATCH reconciler
    // matches BY this id: an entry with a known id updates that row in place;
    // an entry without one is inserted as a fresh row. Preserving it through
    // the edit/toggle flow keeps a milestone's UUID stable across saves — which
    // its AI-Priorities card is keyed on.
    id?: string;
    description: string;
    completed: boolean;
    // Optional ISO "YYYY-MM-DD" local due date (null when unset). Progress is
    // a simple completed/total count now — the old `weight` field is retired.
    due_date?: string | null;
    // Server-assigned ordering index; carried back through PATCH so the row's
    // position is preserved rather than reshuffled.
    sort_order?: number;
}

export interface Rock {
    id: string;
    description: string;
    status: RockStatus;
    visibility: Visibility;
    // Exec-team ("ET") rock — visible to Executive-team members only,
    // regardless of `visibility`.
    is_exec: boolean;
    owner_id: string;
    quarter_id: string;
    // The team this rock belongs to (department name); null for company-wide
    // and Exec-team rocks. Drives the team filter.
    department_name: string | null;
    // Ordered list of milestones (null for rocks created before milestones).
    milestones: RockMilestone[] | null;
    // Server's verdict on whether THIS viewer may change this rock. Any member
    // of the rock's team may UPDATE it (the L10 Rock Review is a team
    // activity); DELETING stays with the owner, the creator, the owner's
    // manager and the team's leadership — hence two flags, not one. Undefined
    // on payloads from an older server, which we read as "allowed" and let the
    // server refuse.
    can_edit?: boolean;
    can_delete?: boolean;
    // Quarter carry-over links, computed by the server per request (never
    // stored on the rock). `carried_from_*` names the rock this one was
    // carried over FROM; `carried_to_*` the rock it was carried INTO. A link
    // shows only while both rocks are live, so deleting the carried rock
    // frees its source to be carried again. Absent on an older server.
    carried_from_rock_id?: string | null;
    carried_from_quarter_id?: string | null;
    carried_from_quarter_name?: string | null;
    carried_to_rock_id?: string | null;
    carried_to_quarter_id?: string | null;
    carried_to_quarter_name?: string | null;
}

// The time window a rock belongs to.
export interface Quarter {
    id: string;
    name: string;
    start_date: string;
    end_date: string;
    is_current: boolean;
}

/**
 * Reconciliation events the host applies to its own `rocks` state. In the L10
 * host this ALSO bumps the poll-suppression window so the 2s live poll doesn't
 * clobber the optimistic edit. `set` carries the partial patches bubbled up
 * from the shared RockDetailModal (status / milestone edits).
 */
export type RockChange =
    | { type: 'upsert'; rock: Rock }
    | { type: 'remove'; id: string }
    | { type: 'set'; id: string; patch: Partial<Rock> };

/** Today's calendar quarter — the default for a new rock. */
const _currentYearQuarter = (): { year: number; quarter: number } => {
    const d = new Date();
    return { year: d.getFullYear(), quarter: Math.floor(d.getMonth() / 3) + 1 };
};

/** Year options for the picker: last year through two years out. */
const _yearOptions = (): number[] => {
    const y = new Date().getFullYear();
    return [y - 1, y, y + 1, y + 2];
};

/**
 * Default view: which quarter the Rocks list should open on.
 *
 * The current quarter (the one flagged `is_current`) when it has at least one
 * rock; otherwise the most-recent quarter that does, by `start_date`. Falls
 * back to 'all' only when no quarter has any rocks — the list then shows
 * everything and the empty state reads "No rocks defined yet."
 *
 * This lands the page on the cycle that's actually in play instead of the
 * full "All quarters" backlog. Kept pure (and exported) so the standalone
 * host can compute the default from freshly-loaded data in one shot and hand
 * it to {@link RocksView} as `initialQuarterFilter`, with no flash of "all".
 */
export const pickDefaultQuarterFilter = (quarters: Quarter[], rocks: Rock[]): string => {
    const quarterIdsWithRocks = new Set(rocks.map(r => r.quarter_id));
    const current = quarters.find(q => q.is_current);
    if (current && quarterIdsWithRocks.has(current.id)) return current.id;
    // ISO datetime strings sort chronologically; newest start_date first.
    const latestWithRocks = [...quarters]
        .filter(q => quarterIdsWithRocks.has(q.id))
        .sort((a, b) => b.start_date.localeCompare(a.start_date))[0];
    return latestWithRocks ? latestWithRocks.id : 'all';
};

const STATUS_LABELS: Record<RockStatus, string> = {
    on_track: 'On Track',
    off_track: 'Off Track',
    done: 'Done',
};

const STATUS_ICON: Record<RockStatus, React.ComponentType<{ size?: number }>> = {
    on_track: Clock,
    off_track: AlertTriangle,
    done: CheckCircle2,
};

// Pill tone for the row status chip (blue / amber / green).
const STATUS_CHIP: Record<RockStatus, string> = {
    on_track: 'bg-blue-50 text-blue-700 border-blue-200',
    off_track: 'bg-amber-50 text-amber-700 border-amber-200',
    done: 'bg-green-50 text-green-700 border-green-200',
};

/** How the rock list is sectioned. 'team' is the org view the page has always
 * shown (Exec Team → departments → Company); 'owner' is the accountability
 * view (each person's rocks under their name — the classic L10 walk-through);
 * 'status' is the priority-of-attention view (Off Track first, Done last).
 * Rocks carry no numeric priority field, so status order IS the priority
 * order here. */
type RockGrouping = 'team' | 'owner' | 'status';

const GROUPING_OPTIONS: { value: RockGrouping; label: string; title: string }[] = [
    { value: 'team', label: 'By team', title: 'Group rocks by team' },
    { value: 'owner', label: 'By person', title: 'Group rocks by owner' },
    { value: 'status', label: 'By status', title: 'Group rocks by status — off-track first' },
];

// Fixed section order for the 'status' grouping: most urgent first.
const STATUS_GROUP_ORDER: RockStatus[] = ['off_track', 'on_track', 'done'];

interface FormState {
    description: string;
    status: RockStatus;
    visibility: Visibility;
    // Exec-team ("ET") rock flag — see Rock.is_exec.
    is_exec: boolean;
    // The team (department name) this rock belongs to. Empty string means
    // company-wide / unset. Ignored when is_exec is true.
    department_name: string;
    owner_email: string;
    // Milestones in the create/edit form. Always a list (we'll
    // send null over the wire only when the list is empty so a saved
    // rock without milestones reads cleanly in the DB).
    milestones: RockMilestone[];
    // The period (calendar year + quarter) this rock is for.
    quarter_year: number;
    quarter_number: number;
}

const EMPTY_FORM: FormState = {
    description: '',
    status: 'on_track',
    visibility: 'department',
    is_exec: false,
    department_name: '',
    owner_email: '',
    milestones: [],
    quarter_year: _currentYearQuarter().year,
    quarter_number: _currentYearQuarter().quarter,
};

/** A quiet "Carried over to / from Qn YYYY" tag on a rock row: a link that
 *  switches the Quarter filter when that quarter is selectable, plain text
 *  when it isn't. */
const CarryLink: React.FC<{ label: string; title: string; onJump?: () => void }> = ({
    label,
    title,
    onJump,
}) =>
    onJump ? (
        <button
            type="button"
            onClick={onJump}
            title={title}
            className="flex-shrink-0 text-xs font-normal text-gray-500 hover:text-brand hover:underline"
        >
            {label}
        </button>
    ) : (
        <span className="flex-shrink-0 text-xs font-normal text-gray-500">{label}</span>
    );

interface RocksViewProps {
    /** 'page' renders full-page chrome (PageContainer + header + quarter/team
     * filters); 'meeting' renders a compact header for embedding in an L10
     * segment (no PageContainer, no filters). */
    mode: 'page' | 'meeting';
    rocks: Rock[];
    people: PersonLite[];
    quarters: Quarter[];
    /** Team scopes for the page team-filter + the create-form Department select
     * and Exec-team option. Omit in meeting mode (team is locked to the meeting). */
    teamScopes?: TeamScope[];
    /** Present only in meeting mode: stamps created rocks with the meeting's
     * team + links them to this L10 so they resurface in future meetings. */
    meetingContext?: MeetingContext;
    /** Host-computed default quarter (pickDefaultQuarterFilter over freshly
     * fetched quarters + rocks). Seeds the internal quarter filter on the page
     * until the user picks one. Page mode only. */
    initialQuarterFilter?: string;
    loading?: boolean;
    error?: string | null;
    /** Host reconciles its own `rocks` state (and, in the L10, suppresses the
     * live poll) in response to these events. */
    onChange: (change: RockChange) => void;
    /** Meeting mode only: fired when a rock's "Add to IDS" (in the shared
     * RockDetailModal) creates an issue, so the L10 host can drop it into the
     * IDS segment immediately. Omitted on the standalone page. */
    onIssueCreated?: (issue: Issue) => void;
}

/**
 * The shared Rocks view — the single source of truth for the standalone Rocks
 * page and the in-meeting L10 "Rock Review" segment. Fully controlled: the host
 * owns fetching + the `rocks` array; this component owns rendering, derivations,
 * ephemeral UI state, the create/edit modal, the AI-draft flow, the shared
 * RockDetailModal, and the write calls — reporting results back via `onChange`.
 */
export const RocksView: React.FC<RocksViewProps> = ({
    mode,
    rocks,
    people,
    quarters,
    teamScopes = [],
    meetingContext,
    initialQuarterFilter,
    loading = false,
    error = null,
    onChange,
    onIssueCreated,
}) => {
    const navigate = useNavigate();
    const { isExec } = useAuth();
    const isMeeting = mode === 'meeting';

    const [modalOpen, setModalOpen] = useState(false);
    const [editing, setEditing] = useState<Rock | null>(null);
    const [form, setForm] = useState<FormState>(EMPTY_FORM);
    const [submitting, setSubmitting] = useState(false);
    // An AI draft in flight ("Draft with AI", or the carry-over re-draft).
    // `aiRequest` numbers the requests: opening or closing the dialog bumps it,
    // so an answer that lands after the user cancelled, or moved on to another
    // rock, is dropped instead of overwriting that rock's form (and, saved,
    // replacing its milestones).
    const [aiSubmitting, setAiSubmitting] = useState(false);
    const aiRequest = useRef(0);
    const dropPendingAiDraft = () => {
        aiRequest.current += 1;
        setAiSubmitting(false);
    };
    // The rock being carried over to a later quarter. Set, the create/edit
    // modal runs in its third mode: a prefilled, editable draft of the rock's
    // next-quarter version, saved through POST /rocks/{id}/carry-over. Page
    // mode only — the L10 embed shows just the current quarter's rocks.
    const [carrying, setCarrying] = useState<Rock | null>(null);

    // Quarters re-fetched after a carry-over created one the host never
    // loaded, merged over the `quarters` prop so the Quarter filter (and the
    // "Carried over to" link) can reach it without a page reload.
    const [refetchedQuarters, setRefetchedQuarters] = useState<Quarter[] | null>(null);
    const allQuarters = useMemo(() => {
        if (!refetchedQuarters) return quarters;
        const fresh = new Map(refetchedQuarters.map(q => [q.id, q]));
        return [
            ...quarters.map(q => fresh.get(q.id) ?? q),
            ...refetchedQuarters.filter(q => !quarters.some(p => p.id === q.id)),
        ];
    }, [quarters, refetchedQuarters]);

    // 'all' or a quarter_id to filter the rock list by period. Seeds
    // from the host-computed default (current quarter, or latest with rocks),
    // falling back to 'all' as a pre-load placeholder. Once the user picks a
    // quarter we stop tracking the incoming default so a late-arriving prop
    // can't clobber their choice.
    const [quarterFilter, setQuarterFilter] = useState<string>(
        initialQuarterFilter ?? 'all',
    );
    const quarterTouched = useRef(false);
    useEffect(() => {
        if (quarterTouched.current) return;
        if (initialQuarterFilter !== undefined) setQuarterFilter(initialQuarterFilter);
    }, [initialQuarterFilter]);

    // Team filter: 'all', '__exec__' (Exec team), or a department name.
    const [teamFilter, setTeamFilter] = useTeamFilter(teamScopes, 'eos.teamFilter.rocks');

    // How the list is sectioned (team / person / status). Defaults to the
    // team view the page has always shown; available in meeting mode too,
    // where "By person" is the classic owner-by-owner Rock Review.
    const [grouping, setGrouping] = useState<RockGrouping>('team');

    // Rock detail / history modal. Only the open flag + which rock is
    // open live here now — the shared RockDetailModal owns its own history
    // fetch, weekly-update form, milestone toggle and Add-to-IDS state.
    const [detailOpen, setDetailOpen] = useState(false);
    const [detailRock, setDetailRock] = useState<Rock | null>(null);

    // Which rocks have their inline milestone panel expanded in the list.
    const [openRockIds, setOpenRockIds] = useState<Set<string>>(new Set());

    const peopleById = useMemo(() => new Map(people.map(p => [p.id, p])), [people]);
    // Department teams the caller can assign a rock to / filter by.
    const departmentScopes = useMemo(
        () => teamScopes.filter(s => s.kind === 'department'),
        [teamScopes],
    );
    const canPickExecTeam = useMemo(
        () => teamScopes.some(s => s.kind === 'exec'),
        [teamScopes],
    );
    // S3: coherent defaults for a NEW rock's Visibility + team pair. A caller
    // who can file under exactly one team (a normal user / single-team manager
    // — that team IS their own department, sourced server-side in
    // /api/eos/me/meeting-scopes) gets Visibility=Department with that team
    // pre-selected. Callers who can file under many teams (admins, org-wide
    // '*' execs) or none get a company-wide rock (Visibility=Company, no team)
    // so the two selects never contradict. Derived from departmentScopes so
    // the value always matches a rendered <option>.
    const newRockDefaults = useMemo<Pick<FormState, 'visibility' | 'department_name'>>(
        () =>
            departmentScopes.length === 1
                ? {
                      visibility: 'department',
                      department_name: departmentScopes[0].value,
                  }
                : { visibility: 'company', department_name: '' },
        [departmentScopes],
    );
    // Rocks shown for the selected period AND team.
    // Team: 'all' = every team; '__exec__' = Exec-team rocks; else a department.
    const visibleRocks = useMemo(
        () =>
            rocks.filter(r => {
                if (quarterFilter !== 'all' && r.quarter_id !== quarterFilter)
                    return false;
                // Slug-equal, not string-equal: the team dropdown offers one
                // canonical spelling per department, but rocks in the live data
                // are tagged with either case-variant ("sales" and "Sales" are
                // both rows). An exact match hid half of them.
                return matchesTeamFilter(teamFilter, r);
            }),
        [rocks, quarterFilter, teamFilter],
    );
    // Rocks in the selected period that the TEAM filter alone is hiding —
    // other teams' rocks, company-wide ones, and (for an exec) Exec-team ones.
    // Counted, never listed: it's a nudge that the view is partial, not a
    // second list. Only ever counts rocks the server already returned to this
    // caller, so it can't reveal anything the filter was protecting. Without
    // it, untagged rocks simply vanish from a team's view and read as data
    // loss.
    const hiddenByTeamFilter = useMemo(() => {
        if (isMeeting || teamFilter === 'all') return 0;
        return rocks.filter(
            r =>
                (quarterFilter === 'all' || r.quarter_id === quarterFilter) &&
                !matchesTeamFilter(teamFilter, r),
        ).length;
    }, [rocks, quarterFilter, teamFilter, isMeeting]);

    // Section the visible rocks per the selected grouping. Each entry is a
    // section card in the render below.
    //  - team (default): Exec Team first, then each department alphabetically,
    //    then the company-wide bucket last.
    //  - owner: one section per rock owner, A→Z, unknown owners last.
    //  - status: fixed buckets in attention order (Off Track → On Track → Done).
    const groupedRocks = useMemo(() => {
        const groups: { key: string; label: string; rocks: Rock[] }[] = [];

        if (grouping === 'owner') {
            // Owners outside the caller-visible people list (e.g. seatless
            // owners) coalesce into ONE trailing 'Unassigned' section rather
            // than one dupe-looking section per unknown id — matching the
            // formatPerson fallback the row's OwnerChip shows for them.
            const UNKNOWN = '__unknown_owner__';
            const byOwner = new Map<string, Rock[]>();
            for (const r of visibleRocks) {
                const key = peopleById.has(r.owner_id) ? r.owner_id : UNKNOWN;
                const arr = byOwner.get(key) ?? [];
                arr.push(r);
                byOwner.set(key, arr);
            }
            return [...byOwner.entries()]
                .map(([key, list]) => ({
                    key,
                    label:
                        key === UNKNOWN
                            ? 'Unassigned'
                            : formatPerson(peopleById.get(key)),
                    rocks: list,
                }))
                .sort((a, b) => {
                    if (a.key === UNKNOWN) return 1;
                    if (b.key === UNKNOWN) return -1;
                    return a.label.localeCompare(b.label);
                });
        }

        if (grouping === 'status') {
            for (const status of STATUS_GROUP_ORDER) {
                const list = visibleRocks.filter(r => r.status === status);
                if (list.length > 0)
                    groups.push({ key: status, label: STATUS_LABELS[status], rocks: list });
            }
            return groups;
        }

        const exec: Rock[] = [];
        // Keyed by department SLUG so rocks tagged "sales" and "Sales" form ONE
        // heading instead of two adjacent ones; the label keeps the canonical
        // spelling of whichever variants showed up.
        const byDept = new Map<string, { label: string; rocks: Rock[] }>();
        const company: Rock[] = [];
        for (const r of visibleRocks) {
            if (r.is_exec) {
                exec.push(r);
            } else if (r.department_name) {
                const slug = deptSlug(r.department_name);
                const group = byDept.get(slug);
                if (group) {
                    group.rocks.push(r);
                    group.label = preferDeptLabel(group.label, r.department_name);
                } else {
                    byDept.set(slug, { label: r.department_name, rocks: [r] });
                }
            } else {
                company.push(r);
            }
        }
        if (exec.length > 0)
            groups.push({ key: EXEC_SCOPE, label: EXEC_SCOPE_LABEL, rocks: exec });
        [...byDept.entries()]
            .sort((a, b) => a[1].label.localeCompare(b[1].label))
            .forEach(([slug, g]) =>
                groups.push({ key: slug, label: g.label, rocks: g.rocks }),
            );
        if (company.length > 0)
            groups.push({ key: '__company__', label: 'Company', rocks: company });
        return groups;
    }, [visibleRocks, grouping, peopleById]);

    // Only surface quarters that still have at least one (visible) rock, plus
    // the current quarter. Quarters are never deleted server-side, so deleting
    // every rock in a quarter — or leftover empty/junk quarters — would
    // otherwise linger in the filter forever. Derived from `rocks`, so it
    // reacts to local deletes without a refetch.
    const selectableQuarters = useMemo(() => {
        const quarterIdsWithRocks = new Set(rocks.map(r => r.quarter_id));
        return allQuarters.filter(
            q => q.is_current || quarterIdsWithRocks.has(q.id),
        );
    }, [allQuarters, rocks]);

    // If the filtered quarter loses all its rocks (e.g. after a delete) it
    // drops out of the dropdown — snap the filter back to "all" so the
    // <select> never points at a hidden value.
    useEffect(() => {
        if (
            quarterFilter !== 'all' &&
            !selectableQuarters.some(q => q.id === quarterFilter)
        ) {
            setQuarterFilter('all');
        }
    }, [selectableQuarters, quarterFilter]);

    const openNew = () => {
        dropPendingAiDraft();
        setEditing(null);
        setCarrying(null);
        const cq = _currentYearQuarter();
        setForm({
            ...EMPTY_FORM,
            visibility: newRockDefaults.visibility,
            department_name: newRockDefaults.department_name,
            owner_email: people[0]?.email ?? '',
            quarter_year: cq.year,
            quarter_number: cq.quarter,
        });
        setModalOpen(true);
    };

    /** May this viewer change this rock? The server decides and sends its
     *  verdict; we only hide the controls so nobody fills in a form the save
     *  will refuse. A rock from an older server carries no flag — treat it as
     *  allowed and let the server answer, which is the behaviour before these
     *  existed. */
    const canEdit = (rock: Rock) => rock.can_edit !== false;
    /** Removing the team's commitment is a narrower grant than updating it. */
    const canDelete = (rock: Rock) => rock.can_delete !== false;

    const openEdit = (rock: Rock) => {
        dropPendingAiDraft();
        setEditing(rock);
        setCarrying(null);
        const owner = peopleById.get(rock.owner_id);
        const cq = _currentYearQuarter();
        setForm({
            description: rock.description,
            status: rock.status,
            visibility: rock.visibility,
            is_exec: rock.is_exec,
            department_name: rock.department_name ?? '',
            owner_email: owner?.email ?? '',
            milestones: rock.milestones ?? [],
            // Period isn't editable here (picker is create-only); defaults unused.
            quarter_year: cq.year,
            quarter_number: cq.quarter,
        });
        setModalOpen(true);
    };

    /** Offered on rocks still in play: a Done rock has nothing left to carry,
     *  and a rock already carried over has its next-quarter version (the
     *  server would answer 409). Page mode only. */
    const canCarryOver = (rock: Rock) =>
        !isMeeting && canEdit(rock) && rock.status !== 'done' && !rock.carried_to_rock_id;

    // Open the create/edit modal as an editable draft of the rock's
    // next-quarter version: same commitment, owner and team, status reset to
    // On Track, and only the unfinished milestones (as new ones — the
    // completed ones stay with the source as its record).
    const openCarryOver = (rock: Rock) => {
        const source = allQuarters.find(q => q.id === rock.quarter_id);
        const target = defaultCarryTarget(source?.start_date ?? null, new Date());
        const owner = peopleById.get(rock.owner_id);
        dropPendingAiDraft();
        setEditing(null);
        setCarrying(rock);
        setForm({
            description: rock.description,
            status: 'on_track',
            visibility: rock.visibility,
            is_exec: rock.is_exec,
            department_name: rock.department_name ?? '',
            // Blank when the owner isn't on this viewer's roster: the server
            // then keeps the source rock's owner.
            owner_email: owner?.email ?? '',
            milestones: carryOverMilestones(rock.milestones, quarterStartISO(target)),
            quarter_year: target.year,
            quarter_number: target.quarter,
        });
        setModalOpen(true);
    };

    const closeModal = () => {
        dropPendingAiDraft();
        setModalOpen(false);
        setEditing(null);
        setCarrying(null);
    };

    // Carry-over dialog derivations. The target must be strictly later than
    // the source rock's quarter (the server refuses anything else with a
    // 400); with the source quarter unknown we let the server decide.
    const carrySourceQuarter = carrying
        ? (allQuarters.find(q => q.id === carrying.quarter_id) ?? null)
        : null;
    const carrySourceYQ = quarterOfISO(carrySourceQuarter?.start_date);
    const carrySourceLabel =
        carrySourceQuarter?.name ?? (carrySourceYQ ? quarterLabel(carrySourceYQ) : null);
    const carryTargetLabel = quarterLabel({
        year: form.quarter_year,
        quarter: form.quarter_number,
    });
    const carryTargetValid =
        !carrySourceYQ ||
        compareQuarters(
            { year: form.quarter_year, quarter: form.quarter_number },
            carrySourceYQ,
        ) > 0;
    const carryCompletedCount = carrying
        ? (carrying.milestones ?? []).filter(m => m.completed).length
        : 0;

    // A carry-over can create its target quarter server-side. Re-fetch the
    // list so the Quarter filter offers it; a failure only costs the filter
    // entry until the next page load, so it is logged, not shown.
    const refreshQuarters = async () => {
        try {
            const res = await client.get<Quarter[]>('/api/eos/quarters/');
            setRefetchedQuarters(res.data);
        } catch (err) {
            console.error('Failed to refresh quarters', err);
        }
    };

    // The "Carried over to / from" links switch the Quarter filter to the
    // other rock's quarter — only when the filter can show it.
    const canJumpToQuarter = (quarterId: string | null | undefined) =>
        !isMeeting && !!quarterId && selectableQuarters.some(q => q.id === quarterId);
    const jumpToQuarter = (quarterId: string | null | undefined) => {
        if (!quarterId || !canJumpToQuarter(quarterId)) return;
        quarterTouched.current = true;
        setQuarterFilter(quarterId);
    };

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        // Submit is disabled too; this covers Enter in a text field.
        if (carrying && !carryTargetValid) return;
        setSubmitting(true);
        try {
            // Send milestones with the rock. Strip empty rows
            // (anyone editing in the form might leave a half-typed one)
            // and send `null` when the list is empty so the DB column
            // stays null rather than storing "[]".
            //
            // Each row carries its optional `due_date` (ISO "YYYY-MM-DD", or
            // null when unset). Progress is a simple completed/total count
            // now, so there's no per-milestone weight to send.
            //
            // Preserve the server-assigned `id`/`sort_order` on rows that
            // already have them so the whole-array PATCH reconciler updates
            // those rows in place (matching by `id`) instead of deleting and
            // recreating them with fresh UUIDs — a milestone's stable UUID is
            // what its AI-Priorities card is keyed on. Newly-added rows (from
            // addMilestone) have no id and are correctly inserted fresh; we
            // omit the id key entirely for them rather than sending
            // `id: undefined`, which the backend treats as "insert".
            const cleanedMilestones = form.milestones
                .map(m => {
                    const cleaned: RockMilestone = {
                        description: m.description.trim(),
                        completed: Boolean(m.completed),
                        due_date: m.due_date || null,
                    };
                    if (m.id) cleaned.id = m.id;
                    if (m.sort_order != null) cleaned.sort_order = m.sort_order;
                    return cleaned;
                })
                .filter(m => m.description.length > 0);
            const milestonesPayload =
                cleanedMilestones.length > 0 ? cleanedMilestones : null;

            // An Exec-team rock has no department (it's the cross-department
            // Exec team, keyed off is_exec); otherwise send the picked team or
            // null for a company-wide rock.
            const departmentPayload = form.is_exec
                ? null
                : form.department_name || null;

            if (carrying) {
                // Creates the rock's next-quarter version; the source rock is
                // left untouched server-side. Same payload shape as a create,
                // except a blank owner (not on this viewer's roster) is left
                // out so the server keeps the source rock's owner.
                const payload: Record<string, unknown> = {
                    description: form.description,
                    status: form.status,
                    visibility: form.visibility,
                    is_exec: form.is_exec,
                    department_name: departmentPayload,
                    milestones: milestonesPayload,
                    quarter_year: form.quarter_year,
                    quarter_number: form.quarter_number,
                };
                if (form.owner_email) payload.owner_email = form.owner_email;
                const res = await client.post<Rock>(
                    `/api/eos/rocks/${carrying.id}/carry-over`,
                    payload,
                );
                const carried = res.data;
                const knownQuarter = allQuarters.find(q => q.id === carried.quarter_id);
                const targetName = knownQuarter?.name ?? carryTargetLabel;
                onChange({ type: 'upsert', rock: carried });
                // Mirror the server's new link onto the source row so its
                // Carry-over button gives way to "Carried over to …" without
                // a reload.
                onChange({
                    type: 'set',
                    id: carrying.id,
                    patch: {
                        carried_to_rock_id: carried.id,
                        carried_to_quarter_id: carried.quarter_id,
                        carried_to_quarter_name: targetName,
                    },
                });
                if (!knownQuarter) void refreshQuarters();
                // The new rock usually sits in a quarter the list isn't
                // showing, so say where it went.
                toast.success(`Rock carried over to ${targetName}.`);
            } else if (editing) {
                // PATCH doesn't take owner_email — resolve to owner_id ourselves.
                const owner = people.find(p => p.email === form.owner_email);
                const payload = {
                    description: form.description,
                    status: form.status,
                    visibility: form.visibility,
                    is_exec: form.is_exec,
                    department_name: departmentPayload,
                    owner_id: owner?.id,
                    milestones: milestonesPayload,
                };
                const res = await client.patch<Rock>(`/api/eos/rocks/${editing.id}`, payload);
                onChange({ type: 'upsert', rock: res.data });
            } else {
                // POST accepts owner_email and resolves it server-side.
                const payload: Record<string, unknown> = {
                    ...form,
                    department_name: departmentPayload,
                    milestones: milestonesPayload,
                };
                if (meetingContext) {
                    // Meeting mode: stamp the meeting's team + link to this L10
                    // so the rock resurfaces in the team's future meetings. The
                    // standalone (page) create omits team stamping and keeps the
                    // form's quarter/visibility/department instead.
                    Object.assign(payload, meetingContext.team);
                    payload.l10_meeting_id = meetingContext.l10MeetingId;
                }
                const res = await client.post<Rock>('/api/eos/rocks/', payload);
                onChange({ type: 'upsert', rock: res.data });
            }
            closeModal();
        } catch (err) {
            console.error(carrying ? 'Failed to carry over rock' : 'Failed to save rock', err);
            // Say what the server said. This used to blame the owner field for
            // every failure, including the 403 a teammate gets on someone
            // else's rock.
            alert(
                errorMessage(
                    err,
                    carrying
                        ? 'Failed to carry over the Rock. Please try again.'
                        : 'Failed to save Rock. Please try again.',
                ),
            );
        } finally {
            setSubmitting(false);
        }
    };

    // Replaces the old "kick the user out to /chat" UX. Calls the
    // new POST /api/eos/rocks/ai-draft endpoint and fills the open
    // create form with a structured SMART rock + milestones the owner
    // can edit in place before saving.
    const draftWithAi = async () => {
        const goal = form.description.trim();
        if (!goal) {
            alert('Type a rough goal in the Description field first, then click Draft with AI.');
            return;
        }
        const request = ++aiRequest.current;
        setAiSubmitting(true);
        try {
            const res = await client.post<{ description: string; milestones: RockMilestone[] }>(
                '/api/eos/rocks/ai-draft',
                { goal },
            );
            if (aiRequest.current !== request) return; // dialog closed or reopened
            setForm(prev => ({
                ...prev,
                description: res.data.description,
                milestones: res.data.milestones ?? [],
            }));
        } catch (err) {
            if (aiRequest.current !== request) return;
            console.error('AI draft failed', err);
            // In a live L10 the embed owns its own navigation — bouncing the
            // facilitator out to /chat would eject them from the running meeting
            // with no way back. Keep the modal open and let them retry or fill
            // the Rock in by hand. Only the standalone Rocks page takes the
            // /chat fallback (where leaving the page is harmless).
            if (isMeeting) {
                alert('AI draft failed. Please try again, or fill in the Rock manually.');
            } else {
                alert(
                    'AI draft failed. Falling back to /chat — your rough goal will be pre-filled there.',
                );
                const fallbackPrompt = `${ROCK_AI_PROMPT}\n\nMy rough goal: ${goal}`;
                closeModal();
                navigate(`/chat?q=${encodeURIComponent(fallbackPrompt)}`);
            }
        } finally {
            if (aiRequest.current === request) setAiSubmitting(false);
        }
    };

    // Carry-over: ask the AI to re-date the rock for the picked quarter
    // (Rocks are written "Noun-Verb-by-Date") and turn the remaining work
    // into milestones dated inside it, from the source rock's milestones and
    // latest weekly updates. It only fills the form; nothing saves until the
    // user does. On failure the form stays as it was — no /chat fallback,
    // which would throw away the carry-over draft.
    const updateCarryWithAi = async () => {
        if (!carrying) return;
        const request = ++aiRequest.current;
        setAiSubmitting(true);
        try {
            const res = await client.post<{ description: string; milestones: RockMilestone[] }>(
                `/api/eos/rocks/${carrying.id}/carry-over/ai-draft`,
                { quarter_year: form.quarter_year, quarter_number: form.quarter_number },
            );
            if (aiRequest.current !== request) return; // dialog closed or reopened
            setForm(prev => ({
                ...prev,
                description: res.data.description,
                // New milestones only: never an id the save could re-attach.
                milestones: (res.data.milestones ?? []).map(m => ({
                    description: m.description,
                    completed: false,
                    due_date: m.due_date ?? null,
                })),
            }));
        } catch (err) {
            if (aiRequest.current !== request) return;
            console.error('AI carry-over draft failed', err);
            alert(
                errorMessage(
                    err,
                    'The AI update failed. Please try again, or edit the Rock by hand.',
                ),
            );
        } finally {
            if (aiRequest.current === request) setAiSubmitting(false);
        }
    };

    // Milestone editor helpers used by the create/edit modal.
    const addMilestone = () => {
        setForm(prev => ({
            ...prev,
            milestones: [...prev.milestones, { description: '', completed: false }],
        }));
    };
    const updateMilestone = (idx: number, patch: Partial<RockMilestone>) => {
        setForm(prev => ({
            ...prev,
            milestones: prev.milestones.map((m, i) => (i === idx ? { ...m, ...patch } : m)),
        }));
    };
    const removeMilestone = (idx: number) => {
        setForm(prev => ({
            ...prev,
            milestones: prev.milestones.filter((_, i) => i !== idx),
        }));
    };

    const onDelete = async (rock: Rock) => {
        if (!confirm(`Delete rock: "${rock.description}"?`)) return;
        try {
            await client.delete(`/api/eos/rocks/${rock.id}`);
            onChange({ type: 'remove', id: rock.id });
            // A carry-over link needs both rocks live. Deleting the carried
            // rock frees its source to be carried again; deleting the source
            // drops the carried rock's "Carried over from" tag.
            const source = rock.carried_from_rock_id
                ? rocks.find(r => r.id === rock.carried_from_rock_id)
                : undefined;
            if (source && source.carried_to_rock_id === rock.id) {
                onChange({
                    type: 'set',
                    id: source.id,
                    patch: {
                        carried_to_rock_id: null,
                        carried_to_quarter_id: null,
                        carried_to_quarter_name: null,
                    },
                });
            }
            if (rock.carried_to_rock_id) {
                onChange({
                    type: 'set',
                    id: rock.carried_to_rock_id,
                    patch: {
                        carried_from_rock_id: null,
                        carried_from_quarter_id: null,
                        carried_from_quarter_name: null,
                    },
                });
            }
        } catch (err) {
            console.error('Delete failed', err);
            alert('Failed to delete Rock.');
        }
    };

    // ── rock detail / weekly-update history ────────────────────────
    // The heavy lifting (history fetch, milestone toggle, weekly-update form,
    // Add-to-IDS) lives in the shared RockDetailModal — the same component the
    // in-meeting L10 session renders. Here we just open/close it and bubble the
    // changes it reports back up to the host (and mirror them onto the open
    // rock so the modal's own props stay coherent if reopened).

    const openDetail = (rock: Rock) => {
        setDetailRock(rock);
        setDetailOpen(true);
    };

    const closeDetail = () => {
        setDetailOpen(false);
        setDetailRock(null);
    };

    // Reflect status / milestone edits made inside the shared modal back into
    // the host's list (via onChange) and the currently-open rock so the row
    // and the modal stay in sync.
    const handleRockChanged = (
        rockId: string,
        patch: { status?: RockStatus; milestones?: RockMilestone[] },
    ) => {
        setDetailRock(prev => (prev && prev.id === rockId ? { ...prev, ...patch } : prev));
        onChange({ type: 'set', id: rockId, patch });
    };

    // Deep-link: /eos/rocks#{rockId} auto-opens the detail modal. Used by
    // L10Session's "Open full" affordance so managers can jump from the
    // mid-meeting Rock Review summary straight into the full history /
    // milestones / IDS view. Page mode only — the meeting embed owns its own
    // navigation and must not hijack the URL hash.
    useEffect(() => {
        if (isMeeting) return;
        if (rocks.length === 0) return;
        const hash = window.location.hash.replace(/^#/, '');
        if (!hash) return;
        const target = rocks.find(r => r.id === hash);
        if (!target) return;
        openDetail(target);
        // Strip the hash so a manual refresh doesn't keep re-opening it.
        window.history.replaceState(
            null,
            '',
            window.location.pathname + window.location.search,
        );
        // openDetail is stable enough here — depending on `rocks` is the
        // signal we care about (modal opens once rocks have loaded).
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [rocks, isMeeting]);

    const toggleRockOpen = (id: string) => {
        setOpenRockIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    // Local "today" for milestone overdue colouring on rows + the expanded
    // milestone panels (recomputed each render).
    const today = todayISO();

    // KPI strip figures over the currently-filtered rocks (current values
    // only — we have no historical baseline, so no trend arrows).
    const rockTotal = visibleRocks.length;
    const onTrackCount = visibleRocks.filter(r => r.status === 'on_track').length;
    const offTrackCount = visibleRocks.filter(r => r.status === 'off_track').length;
    const doneRockCount = visibleRocks.filter(r => r.status === 'done').length;
    const rockPct = (n: number) => (rockTotal > 0 ? Math.round((n / rockTotal) * 100) : 0);

    const newButton = (
        <button
            type="button"
            onClick={openNew}
            className="flex items-center gap-2 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
            data-tour="eos-rocks-new"
        >
            <Plus size={16} /> New Rock
        </button>
    );

    // Grouping toggle — By team / By person / By status segmented control.
    // Rendered on the standalone page AND in the L10 embed, where "By person"
    // is the classic owner-by-owner Rock Review walk.
    const groupToggle = (
        <div
            role="group"
            aria-label="Group rocks by"
            className="inline-flex rounded-md border border-gray-300 overflow-hidden flex-shrink-0"
            data-tour="eos-rocks-grouping"
        >
            {GROUPING_OPTIONS.map((opt, i) => (
                <button
                    key={opt.value}
                    type="button"
                    onClick={() => setGrouping(opt.value)}
                    aria-pressed={grouping === opt.value}
                    title={opt.title}
                    className={`px-3 py-2 text-sm font-medium transition-colors ${
                        i > 0 ? 'border-l border-gray-300' : ''
                    } ${
                        grouping === opt.value
                            ? 'bg-brand text-white'
                            : 'bg-white text-gray-600 hover:bg-gray-50'
                    }`}
                >
                    {opt.label}
                </button>
            ))}
        </div>
    );

    // Page-mode filters (quarter + team). Meeting mode renders none — its data
    // is already scoped to the meeting's team + current cycle.
    const filters = (
        <>
            {/* Filter the rock list by period. Only quarters with live
                rocks (+ the current quarter) appear. */}
            {selectableQuarters.length > 0 && (
                <select
                    value={quarterFilter}
                    onChange={e => {
                        quarterTouched.current = true;
                        setQuarterFilter(e.target.value);
                    }}
                    className="text-sm rounded-md border border-gray-300 px-2 py-2 focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    title="Filter by quarter"
                >
                    <option value="all">All quarters</option>
                    {selectableQuarters.map(q => (
                        <option key={q.id} value={q.id}>
                            {q.name}
                            {q.is_current ? ' (current)' : ''}
                        </option>
                    ))}
                </select>
            )}
            {/* Team filter: Exec team vs a specific department. */}
            {(canPickExecTeam || departmentScopes.length > 0) && (
                <select
                    value={teamFilter}
                    onChange={e => setTeamFilter(e.target.value)}
                    className="text-sm rounded-md border border-gray-300 px-2 py-2 focus:border-brand focus:ring-1 focus:ring-brand outline-none"
                    title="Filter by team"
                    data-tour="eos-rocks-team-filter"
                >
                    <option value="all">All teams</option>
                    {canPickExecTeam && (
                        <option value={EXEC_SCOPE}>{EXEC_SCOPE_LABEL}</option>
                    )}
                    {departmentScopes.map(s => (
                        <option key={s.value} value={s.value}>
                            {s.label}
                        </option>
                    ))}
                </select>
            )}
        </>
    );

    const body = (
        <>
            {loading && <LoadingState label="Loading rocks…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && hiddenByTeamFilter > 0 && (
                <p
                    className="mb-3 text-xs text-gray-500"
                    data-testid="rocks-hidden-by-team"
                >
                    {hiddenByTeamFilter === 1
                        ? '1 more rock in this period belongs to another team or is company-wide, so the Team filter is hiding it.'
                        : `${hiddenByTeamFilter} more rocks in this period belong to other teams or are company-wide, so the Team filter is hiding them.`}{' '}
                    <button
                        type="button"
                        onClick={() => setTeamFilter('all')}
                        className="font-medium text-brand hover:underline"
                    >
                        Show all teams
                    </button>
                </p>
            )}

            {!loading && !error && visibleRocks.length === 0 && (
                <EmptyState
                    title={
                        hiddenByTeamFilter > 0
                            ? 'No rocks for this team in this period.'
                            : quarterFilter === 'all'
                              ? 'No rocks defined yet.'
                              : 'No rocks for this quarter.'
                    }
                    hint={
                        hiddenByTeamFilter > 0
                            ? 'Switch the Team filter to "All teams" to see the rest, or click "New Rock" to add one.'
                            : 'Click "New Rock" above to add one.'
                    }
                />
            )}

            {!loading && !error && visibleRocks.length > 0 && (
                <>
                    {/* Health summary — how the quarter's rocks are tracking. */}
                    <KpiStrip cols={3}>
                        <KpiCard
                            label="On track"
                            hint="Rocks currently on track"
                            leading={<Donut value={rockPct(onTrackCount)} color="#3b82f6" track="#dbeafe" />}
                            value={
                                <span className="flex items-baseline gap-1.5">
                                    {onTrackCount}
                                    <span className="text-xs font-normal text-gray-400">
                                        of {rockTotal} · {rockPct(onTrackCount)}%
                                    </span>
                                </span>
                            }
                        />
                        <KpiCard
                            label="Off track"
                            hint="Rocks currently off track"
                            leading={<Donut value={rockPct(offTrackCount)} color="#f59e0b" track="#fef3c7" />}
                            value={
                                <span className="flex items-baseline gap-1.5">
                                    {offTrackCount}
                                    <span className="text-xs font-normal text-gray-400">
                                        of {rockTotal} · {rockPct(offTrackCount)}%
                                    </span>
                                </span>
                            }
                        />
                        <KpiCard
                            label="Done"
                            hint="Rocks marked done"
                            leading={<Donut value={rockPct(doneRockCount)} color="#22c55e" track="#dcfce7" />}
                            value={
                                <span className="flex items-baseline gap-1.5">
                                    {doneRockCount}
                                    <span className="text-xs font-normal text-gray-400">
                                        of {rockTotal} · {rockPct(doneRockCount)}%
                                    </span>
                                </span>
                            }
                        />
                    </KpiStrip>

                    {/* Rocks sectioned per the grouping toggle (team / person /
                        status), in the To-Dos visual language. */}
                    <div className="space-y-6" data-tour="eos-rocks-list">
                        {groupedRocks.map(group => (
                            <section
                                key={group.key}
                                className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden"
                            >
                                <div className="px-6 py-3 bg-gray-50 border-b border-gray-200 flex items-center gap-2">
                                    <h2 className="text-base font-semibold text-gray-900">
                                        {group.label}
                                    </h2>
                                    <span className="text-xs text-gray-400">
                                        · {group.rocks.length}{' '}
                                        {group.rocks.length === 1 ? 'rock' : 'rocks'}
                                    </span>
                                </div>
                                <ul className="divide-y divide-gray-100">
                                    {group.rocks.map(rock => {
                                        const StatusIcon = STATUS_ICON[rock.status];
                                        const isOpen = openRockIds.has(rock.id);
                                        const milestones = rock.milestones ?? [];
                                        const msTotal = milestones.length;
                                        const msDone = milestones.filter(m => m.completed).length;
                                        const pct = computeMilestoneProgress(rock.milestones);
                                        const next = nextUpcomingMilestone(rock.milestones);
                                        const nextOverdue = next
                                            ? isMilestoneOverdue(next, today)
                                            : false;
                                        return (
                                            <li key={rock.id} className="group">
                                                <div className="px-6 py-3.5 flex items-center gap-4">
                                                    {/* 1. caret — toggles the inline milestone panel */}
                                                    <button
                                                        type="button"
                                                        onClick={() => toggleRockOpen(rock.id)}
                                                        className="text-gray-400 hover:text-brand transition-colors flex-shrink-0"
                                                        aria-expanded={isOpen}
                                                        aria-label={
                                                            isOpen
                                                                ? 'Hide milestones'
                                                                : 'Show milestones'
                                                        }
                                                        title={
                                                            isOpen
                                                                ? 'Hide milestones'
                                                                : 'Show milestones'
                                                        }
                                                    >
                                                        <ChevronRight
                                                            size={16}
                                                            className={`transition-transform ${
                                                                isOpen ? 'rotate-90' : ''
                                                            }`}
                                                        />
                                                    </button>
                                                    {/* 2. status chip */}
                                                    <span
                                                        className={`inline-flex items-center justify-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border min-w-[104px] flex-shrink-0 ${STATUS_CHIP[rock.status]}`}
                                                    >
                                                        <StatusIcon size={13} />
                                                        {STATUS_LABELS[rock.status]}
                                                    </span>
                                                    {/* 3. main: description + optional Company tag.
                                                        Rock names are written "Noun-Verb-Date", so they
                                                        run long by design: they wrap rather than dying at
                                                        an ellipsis, and anything still too long for the
                                                        cell reads in full on hover/focus. Three lines is
                                                        a ceiling, not a reservation — a short name still
                                                        renders as one line — and it's what keeps names
                                                        whole where this cell gets squeezed: the L10 panel
                                                        (wedged between the app nav and the meeting
                                                        sidebar) and the page on a narrow laptop both give
                                                        it well under 200px.
                                                        `flex-wrap` + a 10rem basis drops the Company tag
                                                        onto its own line instead of letting it eat 75px
                                                        of an already-narrow name. */}
                                                    <div className="flex-1 min-w-0">
                                                        <p
                                                            className={`text-sm font-medium flex flex-wrap items-baseline gap-x-2 gap-y-1 ${
                                                                rock.status === 'done'
                                                                    ? 'text-gray-400'
                                                                    : 'text-gray-900'
                                                            }`}
                                                        >
                                                            <ClampedText
                                                                text={rock.description}
                                                                lines={3}
                                                                className="flex-1 basis-40"
                                                            />
                                                            {rock.visibility === 'company' && (
                                                                <span className="flex-shrink-0 text-[10px] font-medium uppercase tracking-wide text-gray-400 border border-gray-200 rounded px-1.5 py-0.5">
                                                                    Company
                                                                </span>
                                                            )}
                                                            {/* Quarter carry-over links, kept quiet:
                                                                they say where the commitment went (or
                                                                came from) and jump the Quarter filter
                                                                there. Page mode only. */}
                                                            {!isMeeting &&
                                                                rock.carried_to_quarter_name && (
                                                                    <CarryLink
                                                                        label={`Carried over to ${rock.carried_to_quarter_name}`}
                                                                        title={`Show the ${rock.carried_to_quarter_name} rocks`}
                                                                        onJump={
                                                                            canJumpToQuarter(
                                                                                rock.carried_to_quarter_id,
                                                                            )
                                                                                ? () =>
                                                                                      jumpToQuarter(
                                                                                          rock.carried_to_quarter_id,
                                                                                      )
                                                                                : undefined
                                                                        }
                                                                    />
                                                                )}
                                                            {!isMeeting &&
                                                                rock.carried_from_quarter_name && (
                                                                    <CarryLink
                                                                        label={`Carried over from ${rock.carried_from_quarter_name}`}
                                                                        title={`Show the ${rock.carried_from_quarter_name} rocks`}
                                                                        onJump={
                                                                            canJumpToQuarter(
                                                                                rock.carried_from_quarter_id,
                                                                            )
                                                                                ? () =>
                                                                                      jumpToQuarter(
                                                                                          rock.carried_from_quarter_id,
                                                                                      )
                                                                                : undefined
                                                                        }
                                                                    />
                                                                )}
                                                        </p>
                                                    </div>
                                                    {/* 4. owner */}
                                                    <div className="hidden sm:block flex-shrink-0">
                                                        <OwnerChip
                                                            name={formatPerson(
                                                                peopleById.get(rock.owner_id),
                                                            )}
                                                        />
                                                    </div>
                                                    {/* 5. milestone progress */}
                                                    <div className="w-[130px] flex-shrink-0 hidden md:block">
                                                        {msTotal > 0 ? (
                                                            <div className="flex items-center gap-2">
                                                                <div className="flex-1 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                                                                    <div
                                                                        className="h-full bg-brand"
                                                                        style={{ width: `${pct}%` }}
                                                                    />
                                                                </div>
                                                                <span className="text-xs text-gray-500 font-medium tabular-nums flex-shrink-0">
                                                                    {msDone}/{msTotal}
                                                                </span>
                                                            </div>
                                                        ) : (
                                                            <span className="text-xs text-gray-400 italic">
                                                                No milestones
                                                            </span>
                                                        )}
                                                    </div>
                                                    {/* 6. next-milestone hint */}
                                                    <div className="w-[128px] flex-shrink-0 text-xs hidden lg:block">
                                                        {rock.status === 'done' ? (
                                                            <span className="text-gray-400">Done</span>
                                                        ) : next ? (
                                                            <span
                                                                className={`flex items-center gap-1 truncate ${
                                                                    nextOverdue
                                                                        ? 'text-red-600 font-medium'
                                                                        : 'text-gray-500'
                                                                }`}
                                                                title={
                                                                    nextOverdue
                                                                        ? `Overdue: ${next.description}`
                                                                        : `Next: ${next.description}`
                                                                }
                                                            >
                                                                <Calendar
                                                                    size={12}
                                                                    className="flex-shrink-0"
                                                                />
                                                                {nextOverdue ? 'Overdue' : 'Next'}:{' '}
                                                                {formatISODate(next.due_date!)}
                                                            </span>
                                                        ) : null}
                                                    </div>
                                                    {/* 7. hover-only actions */}
                                                    <div className="flex items-center gap-1 flex-shrink-0 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
                                                        <button
                                                            type="button"
                                                            onClick={() => openDetail(rock)}
                                                            className="p-1.5 text-gray-400 hover:text-brand hover:bg-brand/5 rounded"
                                                            aria-label="Log weekly update and view history"
                                                            title="Log weekly update and view history"
                                                        >
                                                            <History size={14} />
                                                        </button>
                                                        {canCarryOver(rock) && (
                                                            <button
                                                                type="button"
                                                                onClick={() => openCarryOver(rock)}
                                                                className="p-1.5 text-gray-400 hover:text-brand hover:bg-brand/5 rounded"
                                                                aria-label="Carry over to next quarter"
                                                                title="Carry over to next quarter"
                                                            >
                                                                <CalendarArrowUp size={14} />
                                                            </button>
                                                        )}
                                                        {canEdit(rock) && (
                                                            <button
                                                                type="button"
                                                                onClick={() => openEdit(rock)}
                                                                className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded"
                                                                aria-label="Edit rock"
                                                                title="Edit"
                                                            >
                                                                <Edit2 size={14} />
                                                            </button>
                                                        )}
                                                        {canDelete(rock) && (
                                                            <button
                                                                type="button"
                                                                onClick={() => onDelete(rock)}
                                                                className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded"
                                                                aria-label="Delete rock"
                                                                title="Delete"
                                                            >
                                                                <Trash2 size={14} />
                                                            </button>
                                                        )}
                                                    </div>
                                                </div>

                                                {/* Expanded, read-only milestone panel. */}
                                                {isOpen && (
                                                    <div className="px-6 pb-4">
                                                        <div className="ml-9 rounded-md border border-gray-100 bg-gray-50/60 p-3">
                                                            {msTotal > 0 ? (
                                                                <ul className="space-y-1.5">
                                                                    {milestones.map((m, i) => {
                                                                        const overdue =
                                                                            isMilestoneOverdue(
                                                                                m,
                                                                                today,
                                                                            );
                                                                        return (
                                                                            <li
                                                                                key={i}
                                                                                className="flex items-center gap-2 text-sm"
                                                                            >
                                                                                {m.completed ? (
                                                                                    <CheckCircle2
                                                                                        size={15}
                                                                                        className="text-green-600 shrink-0"
                                                                                    />
                                                                                ) : (
                                                                                    <span
                                                                                        className="inline-block w-3.5 h-3.5 rounded-full border-2 border-gray-300 shrink-0"
                                                                                        aria-hidden="true"
                                                                                    />
                                                                                )}
                                                                                <span
                                                                                    className={`flex-1 ${
                                                                                        m.completed
                                                                                            ? 'line-through text-gray-400'
                                                                                            : 'text-gray-700'
                                                                                    }`}
                                                                                >
                                                                                    {m.description}
                                                                                </span>
                                                                                {m.due_date && (
                                                                                    <span
                                                                                        className={`text-[10px] font-medium shrink-0 ${
                                                                                            overdue
                                                                                                ? 'text-red-600'
                                                                                                : 'text-gray-400'
                                                                                        }`}
                                                                                        title={
                                                                                            overdue
                                                                                                ? 'Overdue'
                                                                                                : 'Due date'
                                                                                        }
                                                                                    >
                                                                                        {formatISODate(
                                                                                            m.due_date,
                                                                                        )}
                                                                                    </span>
                                                                                )}
                                                                            </li>
                                                                        );
                                                                    })}
                                                                </ul>
                                                            ) : (
                                                                <p className="text-xs text-gray-400 italic">
                                                                    No milestones yet.
                                                                </p>
                                                            )}
                                                            {/* Link row — jump into the detail modal or edit. */}
                                                            <div className="flex items-center flex-wrap gap-4 mt-3 pt-2.5 border-t border-gray-100 text-xs">
                                                                <button
                                                                    type="button"
                                                                    onClick={() => openDetail(rock)}
                                                                    className="inline-flex items-center gap-1 font-medium text-brand hover:text-brand-hover"
                                                                >
                                                                    <History size={12} /> Log update
                                                                </button>
                                                                <button
                                                                    type="button"
                                                                    onClick={() => openDetail(rock)}
                                                                    className="inline-flex items-center gap-1 font-medium text-gray-600 hover:text-gray-900"
                                                                >
                                                                    Open details
                                                                </button>
                                                                {canEdit(rock) && (
                                                                    <button
                                                                        type="button"
                                                                        onClick={() => openEdit(rock)}
                                                                        className="inline-flex items-center gap-1 font-medium text-gray-600 hover:text-gray-900"
                                                                    >
                                                                        <Plus size={12} /> Add milestone
                                                                    </button>
                                                                )}
                                                                {canCarryOver(rock) && (
                                                                    <button
                                                                        type="button"
                                                                        onClick={() => openCarryOver(rock)}
                                                                        className="inline-flex items-center gap-1 font-medium text-gray-600 hover:text-gray-900"
                                                                    >
                                                                        <CalendarArrowUp size={12} /> Carry over
                                                                    </button>
                                                                )}
                                                            </div>
                                                        </div>
                                                    </div>
                                                )}
                                            </li>
                                        );
                                    })}
                                </ul>
                            </section>
                        ))}
                    </div>
                </>
            )}
        </>
    );

    // Picking the period. On a carry-over, a milestone date that now falls
    // before the picked quarter is cleared, the same rule the draft opened
    // with: otherwise moving the target from Q4 to Q1 keeps a November date
    // on a rock that starts in January.
    const pickPeriod = (year: number, quarter: number) =>
        setForm(prev => ({
            ...prev,
            quarter_year: year,
            quarter_number: quarter,
            milestones: carrying
                ? clearDueDatesBefore(prev.milestones, quarterStartISO({ year, quarter }))
                : prev.milestones,
        }));

    // The picker's years, plus the form's own year when it falls outside them
    // (a carry-over from a far-future rock) so the select never shows a value
    // it has no option for.
    const periodYearOptions = _yearOptions().includes(form.quarter_year)
        ? _yearOptions()
        : [..._yearOptions(), form.quarter_year].sort((a, b) => a - b);

    const modal = (
        <Modal
            isOpen={modalOpen}
            onClose={closeModal}
            title={
                carrying ? 'Carry over to next quarter' : editing ? 'Edit Rock' : 'New Rock'
            }
            maxWidth="max-w-3xl"
        >
            <form onSubmit={onSubmit} className="space-y-4">
                {/* Carry-over: say what happens to the rock being carried
                    before the user edits its next-quarter version. */}
                {carrying && (
                    <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2.5 text-xs text-gray-600">
                        {carrySourceLabel
                            ? `The ${carrySourceLabel} rock (${STATUS_LABELS[carrying.status]}) stays in ${carrySourceLabel}`
                            : `This rock (${STATUS_LABELS[carrying.status]}) stays in its quarter`}{' '}
                        as the record of what happened; this creates its next-quarter version.
                        {carryCompletedCount > 0 &&
                            (carryCompletedCount === 1
                                ? ' 1 completed milestone stays with it.'
                                : ` ${carryCompletedCount} completed milestones stay with it.`)}
                    </div>
                )}
                <Field label="Description">
                    <input
                        required
                        type="text"
                        value={form.description}
                        onChange={e => setForm({ ...form, description: e.target.value })}
                        className={inputCls}
                        placeholder='e.g. "Launch v2 onboarding flow by end of Q3"'
                        autoFocus
                    />
                    {carrying ? (
                        <button
                            type="button"
                            onClick={updateCarryWithAi}
                            disabled={aiSubmitting || !carryTargetValid}
                            className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:text-brand-hover disabled:opacity-50"
                            title={`Re-date this rock for ${carryTargetLabel} and turn the remaining work into milestones, from its milestones and latest weekly updates`}
                        >
                            <Sparkles size={12} />
                            {aiSubmitting
                                ? 'Updating…'
                                : `Update for ${carryTargetLabel} with AI`}
                        </button>
                    ) : (
                        <button
                            type="button"
                            onClick={draftWithAi}
                            disabled={aiSubmitting}
                            className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-brand hover:text-brand-hover disabled:opacity-50"
                            title="Generate a SMART rock + 3-5 milestones from your rough goal in Description"
                        >
                            <Sparkles size={12} />
                            {aiSubmitting ? 'Drafting…' : 'Draft with AI'}
                        </button>
                    )}
                </Field>

                {/* Editable milestones list. Each row has a completion
                    checkbox, a description, and an optional due date.
                    Progress on the rock is a simple completed/total count. */}
                <Field label={`Milestones (${form.milestones.length})`}>
                    <div className="space-y-2">
                        {form.milestones.length === 0 &&
                            (carrying ? (
                                <p className="text-xs text-gray-500 italic">
                                    No unfinished milestones to carry over. Click{' '}
                                    <strong>Update for {carryTargetLabel} with AI</strong> to plan
                                    the remaining work, or <strong>Add milestone</strong> to add
                                    one manually.
                                </p>
                            ) : (
                                <p className="text-xs text-gray-500 italic">
                                    No milestones yet. Click <strong>Draft with AI</strong> to
                                    generate 3-5 from your goal, or <strong>Add milestone</strong>
                                    to add one manually.
                                </p>
                            ))}
                        {form.milestones.map((m, i) => (
                            <div key={i} className="flex items-center gap-2">
                                <input
                                    type="checkbox"
                                    checked={m.completed}
                                    onChange={e =>
                                        updateMilestone(i, { completed: e.target.checked })
                                    }
                                    className="h-4 w-4 accent-brand"
                                    title="Mark this milestone complete"
                                />
                                <input
                                    type="text"
                                    value={m.description}
                                    onChange={e =>
                                        updateMilestone(i, { description: e.target.value })
                                    }
                                    className={`${inputCls} flex-1 min-w-0`}
                                    placeholder={`Milestone ${i + 1}`}
                                />
                                <input
                                    type="date"
                                    value={m.due_date ?? ''}
                                    onChange={e =>
                                        updateMilestone(i, {
                                            due_date: e.target.value || null,
                                        })
                                    }
                                    className={`${inputCls} !w-40 flex-none`}
                                    title="Due date (optional)"
                                />
                                <button
                                    type="button"
                                    onClick={() => removeMilestone(i)}
                                    className="text-gray-400 hover:text-red-600"
                                    title="Remove milestone"
                                >
                                    <Trash2 size={14} />
                                </button>
                            </div>
                        ))}
                        <button
                            type="button"
                            onClick={addMilestone}
                            className="text-xs font-medium text-brand hover:text-brand-hover inline-flex items-center gap-1"
                        >
                            <Plus size={12} /> Add milestone
                        </button>
                    </div>
                </Field>
                <Field label="Owner">
                    {/* A carry-over whose owner isn't on this viewer's roster
                        may stay blank: the server keeps the source's owner. */}
                    <select
                        required={!carrying}
                        value={form.owner_email}
                        onChange={e => setForm({ ...form, owner_email: e.target.value })}
                        className={inputCls}
                    >
                        <option value="" disabled>
                            {carrying ? 'Keep the current owner' : 'Select owner…'}
                        </option>
                        {people.map(p => (
                            <option key={p.id} value={p.email}>
                                {p.first_name} {p.last_name} — {p.email}
                            </option>
                        ))}
                    </select>
                </Field>
                {/* Which period the rock is for. Create-only — a rock's
                    quarter is fixed once set. Meeting mode locks it to the
                    meeting's current cycle, so the picker only shows on the page.
                    A carry-over picks its target quarter here: strictly after
                    the source rock's, which the server enforces too. Locked
                    while the AI re-drafts so its dates match the quarter. */}
                {!editing && !meetingContext && (
                    <Field
                        label={carrying ? 'Carry over to' : 'Period (which quarter this rock is for)'}
                    >
                        <div className="flex gap-2">
                            <select
                                value={form.quarter_number}
                                disabled={aiSubmitting && carrying !== null}
                                onChange={e =>
                                    pickPeriod(form.quarter_year, parseInt(e.target.value, 10))
                                }
                                className={inputCls}
                            >
                                <option value={1}>Q1 (Jan–Mar)</option>
                                <option value={2}>Q2 (Apr–Jun)</option>
                                <option value={3}>Q3 (Jul–Sep)</option>
                                <option value={4}>Q4 (Oct–Dec)</option>
                            </select>
                            <select
                                value={form.quarter_year}
                                disabled={aiSubmitting && carrying !== null}
                                onChange={e =>
                                    pickPeriod(parseInt(e.target.value, 10), form.quarter_number)
                                }
                                className={inputCls}
                            >
                                {periodYearOptions.map(y => (
                                    <option key={y} value={y}>
                                        {y}
                                    </option>
                                ))}
                            </select>
                        </div>
                        {carrying && !carryTargetValid && (
                            <p className="mt-1 text-xs text-red-600" role="alert">
                                Pick a quarter after {carrySourceLabel}: a rock carries over to a
                                later quarter.
                            </p>
                        )}
                    </Field>
                )}
                <Field label="Visibility">
                    <select
                        value={form.visibility}
                        onChange={e => setForm({ ...form, visibility: e.target.value as Visibility })}
                        className={inputCls}
                    >
                        <option value="company">Company</option>
                        <option value="department">Department</option>
                        <option value="individual">Individual</option>
                    </select>
                </Field>
                {/* Team the rock belongs to. Drives the team filter on the Rocks
                    page. Disabled for Exec-team rocks (the Exec team is
                    cross-department, keyed off the flag below). Meeting mode
                    stamps the team from the meeting, so the picker is hidden. */}
                {!meetingContext && (
                    <Field label="Department (team this rock belongs to)">
                        <select
                            value={form.department_name}
                            onChange={e =>
                                setForm({ ...form, department_name: e.target.value })
                            }
                            className={inputCls}
                            disabled={form.is_exec}
                        >
                            <option value="">Company-wide (no specific team)</option>
                            {departmentScopes.map(s => (
                                <option key={s.value} value={s.value}>
                                    {s.label}
                                </option>
                            ))}
                        </select>
                    </Field>
                )}
                {/* Exec-team ("ET") rock — visible to the Executive team only,
                    regardless of the visibility above. Only exec members can set
                    the flag; non-execs never see the control (and couldn't see
                    the rock afterward anyway). Meeting mode stamps the team from
                    the meeting, so the flag isn't offered there. */}
                {isExec && !meetingContext && (
                    <label className="flex items-start gap-2 text-sm text-gray-700 cursor-pointer">
                        <input
                            type="checkbox"
                            checked={form.is_exec}
                            onChange={e =>
                                setForm({ ...form, is_exec: e.target.checked })
                            }
                            className="mt-0.5 h-4 w-4 rounded border-gray-300 text-brand focus:ring-brand"
                        />
                        <span>
                            <span className="font-medium">Exec Team Rock</span>
                            <span className="block text-xs text-gray-500">
                                Visible to the Executive team only, no matter
                                what visibility is set above.
                            </span>
                        </span>
                    </label>
                )}
                <Field label="Status">
                    <select
                        value={form.status}
                        onChange={e => setForm({ ...form, status: e.target.value as RockStatus })}
                        className={inputCls}
                    >
                        <option value="on_track">On Track</option>
                        <option value="off_track">Off Track</option>
                        <option value="done">Done</option>
                    </select>
                </Field>
                <SubmitButton
                    disabled={submitting || (carrying !== null && !carryTargetValid)}
                    onCancel={closeModal}
                >
                    {carrying ? 'Carry over rock' : editing ? 'Save changes' : 'Create rock'}
                </SubmitButton>
            </form>
        </Modal>
    );

    // Rock detail / weekly-update history — the shared component
    // (same one the in-meeting L10 session renders). It owns its own history
    // fetch, milestone toggle, weekly-update form and Add-to-IDS; we bubble its
    // changes back up to the host.
    const detailModal = (
        <RockDetailModal
            isOpen={detailOpen}
            rockId={detailRock?.id ?? null}
            description={detailRock?.description ?? ''}
            ownerLabel={
                detailRock ? formatPerson(peopleById.get(detailRock.owner_id)) : ''
            }
            status={detailRock?.status ?? 'on_track'}
            milestones={detailRock?.milestones ?? null}
            people={people}
            canEdit={detailRock ? canEdit(detailRock) : true}
            // Carry-over is a page action; the L10 embed passes neither, so
            // the meeting's detail dialog is unchanged.
            onCarryOver={
                !isMeeting && detailRock
                    ? () => {
                          const rock = detailRock;
                          closeDetail();
                          openCarryOver(rock);
                      }
                    : undefined
            }
            carriedToLabel={
                !isMeeting && detailRock?.carried_to_rock_id
                    ? (detailRock.carried_to_quarter_name ?? 'next quarter')
                    : null
            }
            onClose={closeDetail}
            onRockChanged={handleRockChanged}
            onIssueCreated={
                onIssueCreated
                    ? (issue) => onIssueCreated(issue as Issue)
                    : undefined
            }
        />
    );

    if (isMeeting) {
        return (
            <div>
                <div className="flex items-center justify-between mb-4">
                    <div>
                        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                            <Mountain size={18} /> Rocks
                        </h2>
                        <p className="text-sm text-gray-500">
                            Quarterly priorities (90-day goals)
                        </p>
                    </div>
                    <div className="flex items-center gap-2">
                        {groupToggle}
                        {newButton}
                    </div>
                </div>
                {body}
                {modal}
                {detailModal}
            </div>
        );
    }

    return (
        <PageContainer>
            <EosPageHeader
                icon={<Mountain size={20} />}
                title="Rocks"
                subtitle="Quarterly priorities (90-day goals)"
                right={
                    <>
                        {groupToggle}
                        {filters}
                        {newButton}
                    </>
                }
            />
            {body}
            {modal}
            {detailModal}
        </PageContainer>
    );
};
