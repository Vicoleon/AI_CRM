import React, { createContext, useCallback, useContext, useState, useEffect, useRef } from 'react';
import axios from 'axios';
import { googleLogout } from '@react-oauth/google';
import client, { isTokenValid } from '../api/client';
import { instantMs } from '../utils/datetime';
import { eosUiGate } from '../auth/eosAccess';
import { activeToken, clearAuthTokens, clearSupportToken, discardLegacySharedSupportToken, readSupportToken, setSupportToken } from '../brand/storage';
import { loadCurrentProfile } from './profileRequest';
import { syncHomeTokenStorageEvent } from './sessionSync';
import { completeSupportEntry } from './supportEntry';
import { automationStatus } from '../auth/automation';

// ─── Feature access: the SERVER decides, this file only reads ───────────────
//
// Every gate in the app (the copilot, Customer X-Ray, CRM Sync, the four Sales
// Cockpit views, Sales Call Capture, Smart Teams …) resolves on the backend
// through rubik_core/features — a registry of features whose live rosters an
// admin edits in Admin → Feature Access, stored in the database. /api/users/me
// ships the verdicts as `features: { <key>: boolean }`, and the `canAccess*`
// booleans below are those verdicts, nothing more.
//
// DO NOT add a department or email list to this file: who may reach a feature
// is a click on the admin page, and a roster here would silently hide what the
// API is willing to serve. If a new feature needs a nav gate, register it in
// the backend registry and read `features.<key>` here.
//
// What the client still cannot see, and treats as fail-CLOSED (hide the link
// rather than offer one that 403s): the env kill switches (COCKPIT_ENABLED,
// CCS_ENABLED, CUBIE_CRM_SYNC_ENABLED) — Layout.tsx probes
// those surfaces once per app load — and the EOS-seat path in
// require_cockpit_manager, which needs a DB session.

/** Feature keys, exactly as registered in rubik_core/features/registry.py.
 *  backend/tests/test_feature_access.py checks every registered key appears
 *  here, so a new feature cannot ship without the client learning its name.
 *  Not exported (this file must only export components for fast refresh);
 *  consumers pass the key string, typed as `FeatureKey`, to `hasFeature`. */
const FEATURE = {
    copilot: 'copilot',
    xray: 'customer_xray',
    crmSync: 'crm_sync',
    ccs: 'sales_call_capture',
    // The checklist in Google Meet's side panel. Its own page and bundle
    // (src/meetAddon/); in the app it only decides whether Today's Calls
    // points the AE at the panel.
    meetAddon: 'sales_meet_addon',
    cockpit: 'sales_cockpit',
    cockpitManager: 'sales_cockpit_manager',
    cockpitSupervisor: 'sales_cockpit_supervisor',
    cockpitExec: 'sales_cockpit_exec',
    smartTeams: 'smart_teams',
    // Which admins support Smart Teams (another person's raw answers, the
    // skill list). The server ships one combined verdict (the admin role,
    // Smart Teams and this row) under this key and as `smart_teams_support`
    // on the profile; the client reads it, never re-derives it.
    smartTeamsSupport: 'smart_teams_support',
    scheduledTasks: 'scheduled_tasks',
    todos: 'todos',
    // The microphone in the Ask Cubie chat box (stopping it, with its button
    // or Enter, sends the dictated question).
    voiceInput: 'voice_input',
    // The Voice button + its shortcut on every page (registry key `voice_commands`).
    voiceCommands: 'voice_commands',
    // The paperclip in the Ask Cubie chat box (files private to the person who
    // attaches them) and the My private files tab in Assets.
    chatFileUpload: 'chat_file_upload',
    // Meeting write-ups in any shape, saved templates and skills, and routines
    // that run a skill after each meeting: asked for in Ask Cubie, plus the "/"
    // menu in the chat box (registry key `meeting_skills`).
    meetingSkills: 'meeting_skills',
    proposals: 'proposal_generator',
    openCommitments: 'open_commitments',
    closeDateRollup: 'close_date_rollup',
    // The sales daily rhythm: My Day's Morning / Mid-day / End-of-day section
    // and the matching Ask Cubie starter prompts. The team key swaps the
    // prompts for their team-wide version (a named-person roster).
    salesRhythm: 'sales_rhythm',
    salesRhythmTeam: 'sales_rhythm_team',
    // Defer a deal: the Defer button on My Day's deal rows, the Daily rhythm
    // checks and the deal cards on What's On Your Plate, plus My Day's
    // Deferred deals list (where Undefer lives).
    dealDefer: 'deal_defer',
    // Loose Ends: the morning loop-closure scan of each AE's Gmail and Slack
    // against HubSpot (Sales → Loose Ends + the My Day panel). Being on the
    // roster is what starts the scan of that person's mail. The team key is
    // the Sales leaders' owner-attributed aging view (the Team tab).
    looseEnds: 'loop_closure',
    looseEndsTeam: 'loop_closure_team',
    // Overnight-only ("You said you would…" cards in AI Priorities): no nav
    // row or route to gate; the cards arrive in the existing feed. Listed so
    // the client knows every registered key.
    promiseAccountability: 'promise_accountability',
    // Insights (BI dashboards): the "Insights" rail area and its routes, and
    // the narrower per-person tables + person profile inside it.
    insights: 'insights',
    insightsPeople: 'insights_people',
    // Every team's Rocks (except the Exec team's) for the people who keep the
    // company's Rocks. Enforced server-side on the Rock reads/writes and the
    // Rocks page's team list; no nav row or route to gate.
    rocksAllTeams: 'eos_rocks_all_teams',
    // The Restart button on a live L10 (EOS → Meetings). Admins only by
    // default; the server also requires running that meeting and refuses the
    // Exec team's L10.
    l10MeetingRestart: 'l10_meeting_restart',
    // Page tours ("Walk me"): the cards that walk someone through a page the
    // first time they open it, or through a feature added since. Gates the
    // tours themselves, the account menu's replay row and the Settings section.
    pageTours: 'page_tours',
    // What's New: the footer button, the drawer and the Daily Briefing's
    // spotlight bar. Company-wide by default; /api/users/me also folds in the
    // WHATS_NEW_ENABLED kill switch, so false here hides every surface.
    whatsNew: 'whats_new',
    // Cubie for Chrome: the Connect page (/chrome/connect) and the connected
    // browsers card in Settings → General. The extension itself reads the
    // server with its own per-browser key, never this profile.
    chromeExtension: 'chrome_extension',
    // Fathom auto-import: on sign-in (at most hourly), pull the company's
    // recent Fathom recordings into the meeting library. Ships off.
    fathomAutoIngest: 'fathom_auto_ingest',
    // Overnight AI Priorities: decided server-side (the overnight run's
    // audience), so no nav row or route to gate. Listed so the client knows
    // every registered key.
    aiPriorities: 'ai_priorities',
} as const;

export type FeatureKey = (typeof FEATURE)[keyof typeof FEATURE];

/** The server's verdict for one feature. False while the profile is loading,
 *  and false for a key the server did not send — a missing verdict must hide a
 *  surface, never show one. */
function hasFeature(u: User | null | undefined, key: FeatureKey): boolean {
    return u?.features?.[key] === true;
}

// CRUD verdicts for one resource, as served by /api/users/me. Mirrors the
// backend `ResourcePermissions`.
interface ResourcePermissions {
    create: boolean;
    read: boolean;
    update: boolean;
    delete: boolean;
}

interface User {
    id: string;
    email: string;
    name: string;
    picture?: string;
    department?: string;
    roles: string[];
    last_ingest?: string;
    automations_enabled?: boolean;
    is_exec?: boolean;
    // What this user may actually do, resolved server-side from their roles
    // (admins get everything). Keyed by resource: 'assets', 'documents',
    // 'topics', 'users'. Absent only while the profile is still loading.
    permissions?: Record<string, ResourcePermissions>;
    // The server's verdict per gated feature (see the block at the top of this
    // file). Keyed by rubik_core/features registry key. Absent only while the
    // profile is still loading.
    features?: Record<string, boolean>;
    // May read ANOTHER person's raw Smart Teams answers and scores for support.
    // The server's verdict (smart_teams/authz.py `is_support_admin`), never
    // re-derived here.
    smart_teams_support?: boolean;
    // EOS Accountability Chart — departments this user leads (is the Team
    // Manager of). Drives the exec-dashboard filter, the meeting scope
    // picker, and — for a NON-exec — confers team leadership without any
    // Exec-team access. The '*' wildcard is an Exec-only org-wide (CEO) flag.
    managed_departments?: string[];
    // Set while an admin is impersonating this person (read-only). The
    // server's word, from /api/users/me — drives the banner and the way back.
    impersonation?: {
        mode: 'user';
        actor_email: string;
        target_email: string;
        target_name: string;
    } | null;
    /** A live, audited platform support session in another workspace. */
    support?: {
        actor: string;
        workspace_name: string;
    } | null;
    // The workspace this person is in. `company_name` is the company
    // profile's name (Admin → Company setup), "" until an admin saves one, so
    // copy reads `user.workspace?.company_name || 'your company'`.
    workspace?: {
        slug?: string;
        name?: string;
        company_name: string;
    };
    // Enabled product areas are resolved from the workspace's control record.
    // An absent verdict stays closed while the profile loads.
    modules?: {
        core: boolean;
        eos: boolean;
        sales_coach: boolean;
    };
}

// Raw interview answers and numeric scores are private to the person they
// describe, plus the support admins the server names (plan Decision #3). This
// only hides the UI — the server decides per subject in
// rubik_core/smart_teams/authz.py (`can_view_raw_profile`), which is the source
// of truth, and ships its support verdict as `smart_teams_support`. A viewer
// always sees their OWN raw data regardless of what this returns, so pages must
// gate the other-person panel on it, not the own-profile one.
function userCanViewSmartTeamsRawData(u?: User | null): boolean {
    return hasFeature(u, FEATURE.smartTeams) && u?.smart_teams_support === true;
}

interface AuthContextType {
    user: User | null;
    token: string | null;
    setToken: (token: string | null) => void;
    enterSupport: (workspace: string) => Promise<void>;
    exitSupport: () => Promise<void>;
    logout: () => void;
    isLoading: boolean;
    isAdmin: boolean;
    isExec: boolean;
    canAccessEos: boolean;
    // The copilot — may this user use the non-EOS app? Everyone by default; an
    // admin can narrow it, and whoever is left out keeps EOS.
    canAccessFullApp: boolean;
    // UC-45 Customer X-Ray gate.
    canAccessXray: boolean;
    // UC-46 CRM Sync gate. The server also enforces the
    // CUBIE_CRM_SYNC_ENABLED flag, which the client cannot see; endpoints 403
    // when it is off even for users this returns true for.
    canAccessCrmSync: boolean;
    // Cubie Sales Cockpit — the four role-scoped views. Each is "is this the
    // right person", NOT "is the feature on": `COCKPIT_ENABLED` is a server
    // setting the client never sees and every cockpit endpoint 404s while it is
    // off, so anything that OFFERS a cockpit surface asks the server as well.
    canAccessCcs: boolean;
    canAccessCockpit: boolean;
    canAccessCockpitManager: boolean;
    canAccessCockpitSupervisor: boolean;
    canAccessCockpitExec: boolean;
    // Smart Teams — company-wide by design.
    // Compare + Team Builder ride on `canAccessFullApp` as well, not this alone.
    canAccessSmartTeams: boolean;
    // May read ANOTHER person's raw interview answers and scores: the server's
    // support verdict. Your own raw data is never gated on this.
    canViewSmartTeamsRawData: boolean;
    // Scheduled tasks — recurring runs the user sets up in Chat. The server
    // also enforces SCHEDULED_TASKS_ENABLED, which the client cannot see, so
    // the endpoints 404 while it is off even for users this returns true for.
    canAccessScheduledTasks: boolean;
    /** My To-Dos (the personal promises ledger) — registry key `todos`. Gates
     *  the nav row, the /todos route and the "Add to my To-Dos" card action. */
    canAccessTodos: boolean;
    /** Proposal Generator (registry key `proposal_generator`). Reached from a
     *  call's wrap-up, but on its own roster, so it does not imply
     *  `canAccessCcs`. */
    canAccessProposals: boolean;
    // Any registered feature by key — for surfaces without a dedicated boolean.
    hasFeature: (key: FeatureKey) => boolean;
    // Holds assets:create — may add documents to the knowledge base (Assets
    // upload, AI description, Documents-tab one-click ingest). Server-resolved,
    // so the UI disables what the API would reject instead of failing on submit.
    canUploadDocuments: boolean;
    // Team Manager: leads one or more teams via managed_departments WITHOUT
    // being on the Exec team. `managesDept` is case-insensitive.
    isManager: boolean;
    managedDepartments: string[];
    managesDept: (dept?: string | null) => boolean;
    isIngestingTranscripts: boolean;
    ingestionComplete: boolean;
    dismissIngestionBanner: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

/**
 * Read token from localStorage but discard it immediately when it has
 * already expired so we never fire a doomed /api/users/me request.
 */
function getInitialToken(): string | null {
    discardLegacySharedSupportToken();
    const support = readSupportToken();
    if (support && !isTokenValid(support)) clearSupportToken();
    const home = localStorage.getItem('token');
    if (home && !isTokenValid(home)) localStorage.removeItem('token');
    return activeToken();
}

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [user, setUser] = useState<User | null>(null);
    const [token, setToken] = useState<string | null>(getInitialToken);
    const [sessionVersion, setSessionVersion] = useState(0);
    const [isLoading, setIsLoading] = useState(false);
    const [isIngestingTranscripts, setIsIngestingTranscripts] = useState(false);
    const [ingestionComplete, setIngestionComplete] = useState(false);
    const [hasTriggeredIngestion, setHasTriggeredIngestion] = useState(false);
    const currentTokenRef = useRef(token);
    const sessionEpochRef = useRef(0);

    // Invalidate every request from the previous session before React renders
    // the new one. Clearing user here prevents the old workspace from flashing
    // while its replacement /me request is in flight.
    const switchSession = useCallback((nextToken: string | null) => {
        sessionEpochRef.current += 1;
        currentTokenRef.current = nextToken;
        setUser(null);
        setToken(nextToken);
        setSessionVersion(version => version + 1);
        setIsLoading(Boolean(nextToken));
        setIsIngestingTranscripts(false);
        setIngestionComplete(false);
        setHasTriggeredIngestion(false);
    }, []);

    useEffect(() => {
        const onStorage = (event: StorageEvent) => {
            let homeStorage: Storage;
            let supportStorage: Storage | null;
            try {
                homeStorage = window.localStorage;
                supportStorage = window.sessionStorage;
            } catch {
                return;
            }
            syncHomeTokenStorageEvent(
                event, homeStorage, supportStorage, currentTokenRef.current,
                getInitialToken, switchSession,
            );
        };
        window.addEventListener('storage', onStorage);
        return () => window.removeEventListener('storage', onStorage);
    }, [switchSession]);

    const logout = () => {
        googleLogout();
        clearAuthTokens();
        switchSession(null);
    };

    const triggerIngestion = async (userOverride?: User | null) => {
        // The mock-auth preview must not start a real workspace transcript sweep.
        if (import.meta.env.VITE_DEV_LOCAL_PREVIEW === 'true') return;
        if (hasTriggeredIngestion) return;
        
        const currentUser = userOverride !== undefined ? userOverride : user;
        if (!token || !currentUser) return;
        if (automationStatus(currentUser) !== 'enabled') return;

        // Transcript/Fathom ingestion feeds the copilot (Chat, briefing) — skip
        // it for EOS-only users; their /api/ingest calls would 403.
        if (!hasFeature(currentUser, FEATURE.copilot)) return;
        // Never ingest anyone's calls while impersonating (the server refuses
        // it anyway — this just avoids the 403s).
        if (currentUser.impersonation || currentUser.support) return;

        // 1 hour threshold for automatic ingestion
        const THRESHOLD_MS = 3600 * 1000;
        const now = new Date().getTime();
        const lastIngestStr = currentUser.last_ingest;
        
        // `instantMs`, not `new Date`: the profile carries a naive-UTC
        // timestamp, and reading it as local put `last_ingest` HOURS into the
        // future for anyone west of Greenwich — so this gate always said
        // "ran recently" and automatic ingestion never fired for them.
        const lastIngest = instantMs(lastIngestStr);
        if (lastIngest !== null) {
            if (now - lastIngest < THRESHOLD_MS) {
                console.log('Skipping automatic ingestion, last run was less than 1 hour ago.');
                return;
            }
        }

        setHasTriggeredIngestion(true);
        setIsIngestingTranscripts(true);
        const ingestionEpoch = sessionEpochRef.current;
        
        try {
            const apiCalls = [client.post('/api/ingest/meet-transcripts')];

            if (hasFeature(currentUser, FEATURE.fathomAutoIngest)) {
                apiCalls.push(client.post('/api/fathom/ingest'));
            }
            
            await Promise.allSettled(apiCalls);
            
            // Simulation of completion for the subtle indicator/banner
            setTimeout(() => {
                if (ingestionEpoch !== sessionEpochRef.current) return;
                setIsIngestingTranscripts(false);
                setIngestionComplete(true);
                // Also update local user state if possible
                if (currentUser) {
                   setUser({...currentUser, last_ingest: new Date().toISOString()});
                }
            }, 6000);
        } catch (error) {
            console.error('Failed to trigger transcript ingestion:', error);
            if (ingestionEpoch === sessionEpochRef.current) setIsIngestingTranscripts(false);
        }
    };

    const dismissIngestionBanner = () => {
        setIngestionComplete(false);
    };

    const fetchUserProfile = async (requestToken: string, requestEpoch: number) => {
        if (sessionEpochRef.current !== requestEpoch || activeToken() !== requestToken) return;
        setIsLoading(true);
        await loadCurrentProfile({
            token: requestToken,
            load: async () => (await client.get<User>('/api/users/me')).data,
            isCurrentGeneration: () => sessionEpochRef.current === requestEpoch && currentTokenRef.current === requestToken,
            activeToken,
            onProfile: userData => {
                setUser(userData);
                // Trigger ingestion with fresh user data to avoid stale state issues.
                triggerIngestion(userData);
            },
            onUnauthorized: () => {
                // A support token can expire or be revoked while the home
                // session stays valid. The interceptor clears only the token
                // it sent; restore the home profile rather than signing out.
                const fallback = getInitialToken();
                if (fallback && fallback !== requestToken) {
                    switchSession(fallback);
                } else {
                    logout();
                }
            },
            onError: error => console.error('Failed to fetch user profile:', error),
            onSettled: () => setIsLoading(false),
        });
    };

    useEffect(() => {
        if (!token || currentTokenRef.current !== token) return;
        // Double-check validity in case token expired between renders
        if (!isTokenValid(token)) {
            // A storage change is external to React. Defer the state update
            // until after this effect and re-check that this is still the
            // session that was found expired.
            queueMicrotask(() => {
                if (currentTokenRef.current !== token) return;
                const fallback = getInitialToken();
                if (fallback && fallback !== token) switchSession(fallback);
                else logout();
            });
            return;
        }
        // The local `mock-token` reads /api/users/me like any other token, so
        // the page shows the identity the server signs in and authorises:
        // CUBIE_DEV_USER_EMAIL (default dev@acme.example).
        const requestEpoch = sessionEpochRef.current;
        queueMicrotask(() => {
            if (sessionEpochRef.current === requestEpoch && currentTokenRef.current === token) {
                void fetchUserProfile(token, requestEpoch);
            }
        });
    }, [token, sessionVersion]);

    const handleSetToken = (newToken: string | null) => {
        clearSupportToken();
        if (newToken) {
            localStorage.setItem('token', newToken);
        } else {
            localStorage.removeItem('token');
        }
        switchSession(newToken);
    };

    const enterSupport = async (workspace: string): Promise<void> => {
        if (readSupportToken()) throw new Error('Exit the current support session first.');
        const homeToken = currentTokenRef.current;
        const homeEpoch = sessionEpochRef.current;
        if (!homeToken || !isTokenValid(homeToken) || activeToken() !== homeToken) {
            throw new Error('A current home session is required to enter support.');
        }
        const result = client.post<{ token: string; expires_at: string; workspace: { slug: string; name: string } }>(
            '/api/platform/support/enter', { workspace },
        ).then(response => response.data);
        await completeSupportEntry({
            result,
            isCurrentHomeSession: () => sessionEpochRef.current === homeEpoch
                && currentTokenRef.current === homeToken
                && activeToken() === homeToken
                && !readSupportToken(),
            isTokenValid,
            storeToken: setSupportToken,
            activate: supportToken => {
                switchSession(supportToken);
                window.location.assign('/');
            },
            // Use the returned support bearer explicitly. The normal client
            // interceptor now reads the replacement home token, so it cannot
            // revoke this abandoned support session.
            revoke: async supportToken => {
                await axios.post('/api/platform/support/exit', null, {
                    headers: { Authorization: `Bearer ${supportToken}` },
                    timeout: 5000,
                });
            },
        });
    };

    const exitSupport = async (): Promise<void> => {
        if (!readSupportToken()) return;
        await client.post('/api/platform/support/exit');
        clearSupportToken();
        switchSession(getInitialToken());
        window.location.assign('/');
    };

    const isAdmin = user?.roles?.includes('admin') || false;
    const isExec = user?.is_exec ?? false;
    const canAccessEos = eosUiGate({ token, user, isLoading }).showNav;
    // Every gate below is the server's verdict from /api/users/me. All false
    // while `user` is still loading (route guards wait on isLoading/user
    // before judging — see App.tsx).
    const canAccessFullApp = hasFeature(user, FEATURE.copilot);
    const canAccessXray = hasFeature(user, FEATURE.xray);
    const canAccessCrmSync = hasFeature(user, FEATURE.crmSync);
    const canAccessCcs = hasFeature(user, FEATURE.ccs);
    const canAccessCockpit = hasFeature(user, FEATURE.cockpit);
    const canAccessCockpitManager = hasFeature(user, FEATURE.cockpitManager);
    const canAccessCockpitSupervisor = hasFeature(user, FEATURE.cockpitSupervisor);
    const canAccessCockpitExec = hasFeature(user, FEATURE.cockpitExec);
    const canAccessSmartTeams = hasFeature(user, FEATURE.smartTeams);
    const canAccessScheduledTasks = hasFeature(user, FEATURE.scheduledTasks);
    const canAccessTodos = hasFeature(user, FEATURE.todos);
    const canAccessProposals = hasFeature(user, FEATURE.proposals);
    const canViewSmartTeamsRawData = userCanViewSmartTeamsRawData(user);
    // Trust the server's verdict only — never re-derive it from role names
    // here. False while the profile loads, so the button never flashes enabled.
    const canUploadDocuments = user?.permissions?.assets?.create ?? false;
    // Team Managers: non-empty managed_departments minus the '*' wildcard
    // (Exec-only, org-wide) and the 'general' catch-all — leadership over
    // their team(s) without Exec-team access.
    const managedDepartments = (user?.managed_departments ?? []).filter(
        d => !!d && d !== '*' && d.toLowerCase() !== 'general',
    );
    const isManager = managedDepartments.length > 0;
    const managesDept = (dept?: string | null): boolean =>
        !!dept && managedDepartments.some(d => d.toLowerCase() === dept.toLowerCase());

    return (
        <AuthContext.Provider value={{
            user, token, setToken: handleSetToken, enterSupport, exitSupport, logout, isLoading, isAdmin, isExec, canAccessEos, canAccessFullApp,
            canAccessXray, canAccessCrmSync, canUploadDocuments,
            canAccessCcs,
            canAccessCockpit, canAccessCockpitManager, canAccessCockpitSupervisor, canAccessCockpitExec,
            canAccessSmartTeams, canViewSmartTeamsRawData,
            canAccessScheduledTasks,
            canAccessTodos,
            canAccessProposals,
            hasFeature: (key: FeatureKey) => hasFeature(user, key),
            isManager, managedDepartments, managesDept,
            isIngestingTranscripts, ingestionComplete, dismissIngestionBanner
        }}>
            {children}
        </AuthContext.Provider>
    );
};

// The hook lives beside its provider on purpose: every consumer imports both
// from here, and splitting them would only move this rule's complaint.
// eslint-disable-next-line react-refresh/only-export-components
export const useAuth = () => {
    const context = useContext(AuthContext);
    if (!context) throw new Error('useAuth must be used within an AuthProvider');
    return context;
};
