import React, { useState, useEffect, useRef, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { type AdminTab, adminTabFromSearch } from './adminSections';
import { Users, Settings, Save, Trash2, Shield, CheckCircle2, AlertCircle, Edit, X, Plus, Search, Filter, MoreHorizontal, User, Key, Lock, ChevronRight, Download, Clock, UserMinus } from 'lucide-react';
import { PrioritiesTab } from './admin/PrioritiesTab';
import { ChatModelTab } from './admin/ChatModelTab';
import { SalesModelsTab } from './admin/SalesModelsTab';
import { LooseEndsTab } from './admin/LooseEndsTab';
import { CompanyTab } from './admin/CompanyTab';
import { WhatsNewTab } from './admin/WhatsNewTab';
import { FeedbackTab } from './admin/FeedbackTab';
import { LabelsTab } from './admin/LabelsTab';
import { AICostTab } from './admin/AICostTab';
import { L10MeetingsTab } from './admin/L10MeetingsTab';
import { DepartmentFoldersSection } from './admin/DepartmentFoldersSection';
import { FeatureAccessTab } from './admin/FeatureAccessTab';
import { HubSpotSettingsCard } from './admin/HubSpotSettingsCard';
import { WorkspaceOperationsCard } from './admin/WorkspaceOperationsCard';
import { parseWorkspaceIdentity, type WorkspaceIdentity } from './admin/workspaceIdentity';
import { WorkspaceAIProvidersCard } from './admin/WorkspaceAIProvidersCard';
import { Departments } from './Departments';
import { motion, AnimatePresence } from 'framer-motion';
import client from '../api/client';
import { formatInstant } from '../utils/datetime';
import { useAuth } from '../context/AuthContext';
import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

function cn(...inputs: any[]) {
    return twMerge(clsx(inputs));
}

interface ResourcePermissions {
    create: boolean;
    read: boolean;
    update: boolean;
    delete: boolean;
}

interface Role {
    name: string;
    description: string;
    permissions: Record<string, ResourcePermissions>;
}

interface UserProfile {
    email: string;
    name: string;
    roles: string[];
    department: string;
    created_at?: string;
    last_login?: string;
    workspace_groups?: string[];
    is_exec?: boolean;
    // EOS Accountability Chart — departments this user is the team
    // owner / integrator of. Used by /api/eos/me/meeting-scopes to
    // populate the L10 scope dropdown.
    managed_departments?: string[];
}

interface WorkspaceConfig {
    settings: {
        workspace_sync_enabled: boolean;
        multi_gmail_enabled?: boolean;
        // Hours before a signed-in session expires and Cubie requires a new
        // Google sign-in. null/absent = backend default (5h).
        session_lifetime_hours?: number | null;
    };
    mappings: Record<string, string[]>;
}

/* The Administration sub-nav, grouped.

   Twelve flat rows made every screen look equally weighted, so finding "the
   place where Drive labels are weighted" meant reading the whole list. These
   four groups are the mental model an admin already has: who can get in
   (Access), how the AI behaves (AI Settings), the Drive content that feeds it
   (Productivity), and the org-level odds and ends (Admin).

   Departments used to be its own left-panel route; it lives here now, and
   /departments redirects to `?tab=departments`.

   This array is the single source of truth: the tab union, the `?tab=`
   allow-list and the rendered nav are all derived from it, so a new tab cannot
   be added to the nav and forgotten in the deep-link validation (they used to
   be two hand-maintained lists). */
/* The Administration sections now live in pages/adminSections, because the
   app's left nav panel renders the tree and this page derives its tab union
   and ?tab= allow-list from the same list — one definition, two consumers, no
   drift. */

// Safe default so the Google Workspace tab is always interactive (toggle /
// inputs) even if the settings endpoint fails to load. The handlers guard on
// `prev` being non-null, so a null config silently freezes the whole tab.
const DEFAULT_WORKSPACE_CONFIG: WorkspaceConfig = {
    settings: { workspace_sync_enabled: false, multi_gmail_enabled: false },
    mappings: {},
};

const RESOURCE_TYPES = ['documents', 'topics', 'users', 'assets'];
const PERMISSION_TYPES = ['create', 'read', 'update', 'delete'];

interface Group {
    id: string;
    name: string;
    description?: string;
    user_count?: number;
}

interface AccessPolicy {
    id: number;
    user_email?: string;
    group_id?: string;
    group_name?: string;
    department?: string;
    topic?: string;
    can_read: boolean;
    can_update: boolean;
}

export const Admin: React.FC = () => {
    const { user, token } = useAuth();
    const identity = `${user?.workspace?.slug ?? ''}:${user?.email ?? ''}:${token ?? ''}`;
    // Reset loaded settings and in-flight response handlers when a support
    // session or authenticated user changes while Admin remains mounted.
    return <AdminContent key={identity} />;
};

const AdminContent: React.FC = () => {
    const { isAdmin, user } = useAuth();
    const [searchParams] = useSearchParams();
    /* DERIVED FROM THE URL, not mirrored in state. The section links live in the
       app's left nav panel now, so an external navigation is the only thing that
       moves the section — a useState copy would just be a second source of truth
       to fall out of step. An unknown or absent ?tab= resolves to Users rather
       than rendering an empty pane. */
    const activeTab: AdminTab = adminTabFromSearch(searchParams.toString());
    const [users, setUsers] = useState<UserProfile[]>([]);
    const [roles, setRoles] = useState<Role[]>([]);
    const [groups, setGroups] = useState<Group[]>([]);
    const [policies, setPolicies] = useState<AccessPolicy[]>([]);
    const [topics, setTopics] = useState<string[]>([]);
    const [workspaceConfig, setWorkspaceConfig] = useState<WorkspaceConfig | null>(DEFAULT_WORKSPACE_CONFIG);
    const [workspaceIdentity, setWorkspaceIdentity] = useState<WorkspaceIdentity | null>(null);
    const [workspaceGroups, setWorkspaceGroups] = useState<{ email: string, name: string }[]>([]);
    const [departments, setDepartments] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [savingUser, setSavingUser] = useState(false);

    // Modals
    const [showAddUserModal, setShowAddUserModal] = useState(false);
    const [showEditUserModal, setShowEditUserModal] = useState(false);
    // Additional EOS teams for the user being edited — the teams whose L10
    // roster currently carries them (see backend `_authz.roster_dept_names`).
    // Fetched per-user when the modal opens rather than with the user list:
    // it needs a roster scan, and it's only ever needed for one person at a
    // time. `null` = not loaded yet, `[]` = loaded and they're on no other
    // team's L10 (which is most people).
    const [eosTeams, setEosTeams] = useState<
        { department: string; granted: boolean }[] | null
    >(null);
    const [eosTeamsSaving, setEosTeamsSaving] = useState(false);
    const [showAddRoleModal, setShowAddRoleModal] = useState(false);
    const [offboardingUser, setOffboardingUser] = useState<UserProfile | null>(null);
    const [showEditRoleModal, setShowEditRoleModal] = useState(false);

    // Form states
    const [newUser, setNewUser] = useState<{
        email: string;
        name: string;
        roles: string[];
        department: string;
        is_exec: boolean;
        managed_departments: string[];
    }>({
        email: '',
        name: '',
        roles: ['user'],
        department: 'general',
        is_exec: false,
        managed_departments: [],
    });
    const [editingUser, setEditingUser] = useState<UserProfile | null>(null);
    const [newRole, setNewRole] = useState<Role>({
        name: '',
        description: '',
        permissions: RESOURCE_TYPES.reduce((acc, resource) => ({
            ...acc,
            [resource]: { create: false, read: false, update: false, delete: false }
        }), {})
    });
    const [editingRole, setEditingRole] = useState<Role | null>(null);

    const [message, setMessage] = useState<{ type: 'success' | 'error', text: string } | null>(null);

    useEffect(() => {
        if (isAdmin) {
            fetchData();
        }
    }, [isAdmin]);

    const fetchWorkspaceGroups = async () => {
        try {
            const groupsRes = await client.get('/api/users/workspace/groups');
            setWorkspaceGroups(groupsRes.data);
            return groupsRes.data;
        } catch (err) {
            console.error('Failed to fetch workspace groups:', err);
            return [];
        }
    };

    const fetchData = async () => {
        setLoading(true);
        setWorkspaceIdentity(null);

        // Load each admin section independently. A single failing endpoint
        // (e.g. a transient DB blip on /topics) must NOT blank the whole page
        // — that previously left workspaceConfig null and froze the Google
        // Workspace tab's toggle and inputs. allSettled lets the rest render.
        const sections = [
            { label: 'users', req: client.get('/api/users/'), apply: (d: any) => setUsers(d) },
            { label: 'workspace settings', req: client.get('/api/users/workspace/settings'), apply: (d: any) => setWorkspaceConfig(d) },
            { label: 'workspace identity', req: client.get('/api/users/workspace/identity'), apply: (d: unknown) => setWorkspaceIdentity(parseWorkspaceIdentity(d)) },
            { label: 'roles', req: client.get('/api/users/roles/list'), apply: (d: any) => setRoles(d) },
            { label: 'departments', req: client.get('/api/departments/'), apply: (d: any) => setDepartments(d) },
            { label: 'groups', req: client.get('/api/groups/'), apply: (d: any) => setGroups(d) },
            { label: 'policies', req: client.get('/api/policies/'), apply: (d: any) => setPolicies(d) },
            { label: 'topics', req: client.get('/api/topics/'), apply: (d: any) => setTopics(d) },
        ];

        const results = await Promise.allSettled(sections.map((s) => s.req));

        const failed: string[] = [];
        results.forEach((res, i) => {
            if (res.status === 'fulfilled') {
                sections[i].apply(res.value.data);
            } else {
                failed.push(sections[i].label);
                console.error(`Failed to load admin section "${sections[i].label}":`, res.reason);
            }
        });

        // Fetch workspace groups if enabled (only when settings loaded cleanly)
        const configRes = results[1];
        if (configRes.status === 'fulfilled' && configRes.value.data?.settings?.workspace_sync_enabled) {
            await fetchWorkspaceGroups();
        }

        if (failed.length > 0) {
            setMessage({ type: 'error', text: `Failed to load: ${failed.join(', ')}. Other sections still loaded — try again or check the server.` });
        }

        setLoading(false);
    };

    // Auto-fetch groups when entering workspace tab if enabled
    useEffect(() => {
        if (activeTab === 'workspace' && workspaceConfig?.settings.workspace_sync_enabled && workspaceGroups.length === 0) {
            fetchWorkspaceGroups();
        }
    }, [activeTab, workspaceConfig?.settings.workspace_sync_enabled]);



    const handleCreateUser = async () => {
        setSavingUser(true);
        try {
            const payload = {
                ...newUser,
                // '*' (org-wide) is Exec-only; strip it for non-execs.
                managed_departments: newUser.is_exec
                    ? newUser.managed_departments
                    : newUser.managed_departments.filter(d => d !== '*'),
            };
            await client.post('/api/users/', payload);
            setShowAddUserModal(false);
            setNewUser({ email: '', name: '', roles: ['user'], department: 'general', is_exec: false, managed_departments: [] });
            setMessage({ type: 'success', text: `Registered new user: ${newUser.email}` });
            await fetchData();
        } catch (error: any) {
            setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to create user.' });
        } finally {
            setSavingUser(false);
        }
    };

    const handleEditUser = async () => {
        if (!editingUser) return;
        setSavingUser(true);
        try {
            await client.patch(`/api/users/${editingUser.email}`, {
                name: editingUser.name,
                department: editingUser.department,
                roles: editingUser.roles,
                is_exec: editingUser.is_exec ?? false,
                // Team-Manager departments are sent for ALL users now
                // (decoupled from Exec). Only an exec may hold the '*'
                // org-wide wildcard, so strip it for non-execs.
                managed_departments: (editingUser.is_exec ?? false)
                    ? (editingUser.managed_departments ?? [])
                    : (editingUser.managed_departments ?? []).filter(d => d !== '*'),
            });
            // Close the modal as soon as the save succeeds, THEN refresh in the
            // background — otherwise the page-level reload spinner runs while the
            // modal sits open and inert, which reads as "nothing happened".
            // Additional EOS teams live on user_preferences, not users, so
            // they save through their own endpoint. Only sent when the section
            // actually loaded — never send [] for a user whose teams we never
            // fetched, which would silently revoke every grant they had.
            if (eosTeams !== null) {
                await client.put(`/api/users/${editingUser.email}/eos-teams`, {
                    departments: eosTeams.filter(t => t.granted).map(t => t.department),
                });
            }
            const updatedEmail = editingUser.email;
            setShowEditUserModal(false);
            setEditingUser(null);
            setEosTeams(null);
            setMessage({ type: 'success', text: `Updated user: ${updatedEmail}` });
            await fetchData();
        } catch (error: any) {
            setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to update user.' });
        } finally {
            setSavingUser(false);
        }
    };

    const handleCreateRole = async () => {
        try {
            await client.post('/api/users/roles', newRole);
            await fetchData();
            setShowAddRoleModal(false);
            setNewRole({
                name: '',
                description: '',
                permissions: RESOURCE_TYPES.reduce((acc, resource) => ({
                    ...acc,
                    [resource]: { create: false, read: false, update: false, delete: false }
                }), {})
            });
            setMessage({ type: 'success', text: `Added role: ${newRole.name}` });
        } catch (error) {
            setMessage({ type: 'error', text: 'Failed to add role.' });
        }
    };

    const handleUpdateRole = async () => {
        if (!editingRole) return;
        try {
            await client.patch(`/api/users/roles/${editingRole.name}`, {
                description: editingRole.description,
                permissions: editingRole.permissions
            });
            await fetchData();
            setShowEditRoleModal(false);
            setEditingRole(null);
            setMessage({ type: 'success', text: `Updated role: ${editingRole.name}` });
        } catch (error) {
            setMessage({ type: 'error', text: 'Failed to update role.' });
        }
    };

    const handleDeleteRole = async (roleName: string) => {
        if (!confirm(`Are you sure you want to delete the role "${roleName}"?`)) return;
        try {
            await client.delete(`/api/users/roles/${roleName}`);
            await fetchData();
            setMessage({ type: 'success', text: `Deleted role: ${roleName}` });
        } catch (error) {
            setMessage({ type: 'error', text: 'Failed to delete role.' });
        }
    };

    const handleSaveWorkspaceSettings = async () => {
        if (!workspaceConfig) return;
        setSaving(true);
        try {
            await client.patch('/api/users/workspace/settings', workspaceConfig.settings);
            await client.put('/api/users/workspace/mappings', workspaceConfig.mappings);
            if (workspaceConfig.settings.workspace_sync_enabled) {
                await fetchWorkspaceGroups();
            }
            setMessage({ type: 'success', text: 'Workspace settings saved successfully.' });
        } catch (error) {
            setMessage({ type: 'error', text: 'Failed to save workspace settings.' });
        } finally {
            setSaving(false);
        }
    };



    if (!isAdmin) {
        return (
            <div className="flex flex-col items-center justify-center min-h-[60vh]">
                <Shield className="h-16 w-16 text-red-500 mb-4" />
                <h1 className="text-2xl font-bold text-gray-900">Access Denied</h1>
                <p className="text-gray-500">You do not have administrator privileges.</p>
            </div>
        );
    }

    return (
        <div className="max-w-6xl mx-auto px-4 py-8">
            <div data-tour="admin-header" className="mb-8">
                <h1 className="text-3xl font-bold text-gray-900">Administration</h1>
                <p className="text-gray-500">Manage users, roles, and external integrations.</p>
            </div>

            {/* Message Alert */}
            <AnimatePresence>
                {message && (
                    <motion.div
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: 'auto' }}
                        exit={{ opacity: 0, height: 0 }}
                        className={cn(
                            "mb-6 p-4 rounded-lg flex items-center gap-3",
                            message.type === 'success' ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"
                        )}
                    >
                        {message.type === 'success' ? <CheckCircle2 className="h-5 w-5" /> : <AlertCircle className="h-5 w-5" />}
                        <span>{message.text}</span>
                        <button onClick={() => setMessage(null)} className="ml-auto text-sm underline">Dismiss</button>
                    </motion.div>
                )}
            </AnimatePresence>

            {/* No section nav here: the app's left panel is it. */}
            <div>
                <section className="min-w-0">
                    <div data-tour="admin-section" className="bg-white rounded-2xl shadow-sm border border-gray-100 p-6">
                    {loading ? (
                        <div className="flex items-center justify-center py-12">
                            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand"></div>
                        </div>
                    ) : (
                        <AnimatePresence mode="wait">
                            {activeTab === 'users' && (
                                <UsersTab
                                    users={users}
                                    roles={roles}
                                    onAddUser={() => setShowAddUserModal(true)}
                                    onEditUser={(user) => {
                                        setEditingUser(user);
                                        setShowEditUserModal(true);
                                        // Fetch here, not in an effect keyed on
                                        // editingUser: a per-user fetch in an
                                        // effect trips the set-state-in-effect
                                        // lint, and this is a click anyway.
                                        setEosTeams(null);
                                        setEosTeamsSaving(true);
                                        client
                                            .get(`/api/users/${user.email}/eos-teams`)
                                            .then(r => setEosTeams(r.data.eligible ?? []))
                                            .catch(() => setEosTeams([]))
                                            .finally(() => setEosTeamsSaving(false));
                                    }}
                                    onOffboardUser={(user) => setOffboardingUser(user)}
                                    onDeleteUser={async (email) => {
                                        if (confirm(`Delete ${email}? This is for junk/test accounts — a real person should be OFFBOARDED so their work is handed to a successor.`)) {
                                            try {
                                                await client.delete(`/api/users/${email}`);
                                            } catch (err: unknown) {
                                                const e = err as { response?: { status?: number; data?: { detail?: string } } };
                                                if (e.response?.status !== 409) throw err;
                                                // The account has history (priorities cards or owned
                                                // docs). A real person is offboarded; a junk or test
                                                // account that the nightly run has been writing empty
                                                // cards for is deleted anyway, on a second, explicit yes.
                                                const detail = e.response.data?.detail ?? 'This user has history.';
                                                if (!confirm(`${detail}\n\nOnly continue if ${email} is a junk or test account, not a person. Delete it anyway?`)) {
                                                    return;
                                                }
                                                try {
                                                    await client.delete(`/api/users/${email}`, { params: { force: true } });
                                                } catch {
                                                    alert(`${email} could not be deleted. If they own documents, offboard them instead so a successor inherits them.`);
                                                    return;
                                                }
                                            }
                                            await fetchData();
                                        }
                                    }}
                                />
                            )}
                            {activeTab === 'roles' && (
                                <RolesTab
                                    roles={roles}
                                    onAddRole={() => setShowAddRoleModal(true)}
                                    onEditRole={(role) => {
                                        setEditingRole(role);
                                        setShowEditRoleModal(true);
                                    }}
                                    onDeleteRole={handleDeleteRole}
                                />
                            )}
                            {activeTab === 'workspace' && (
                                <WorkspaceTab
                                    workspaceConfig={workspaceConfig}
                                    workspaceIdentity={workspaceIdentity}
                                    workspaceSlug={user?.workspace?.slug ?? null}
                                    setWorkspaceConfig={setWorkspaceConfig}
                                    workspaceGroups={workspaceGroups}
                                    roles={roles}
                                    saving={saving}
                                    onSave={handleSaveWorkspaceSettings}
                                />
                            )}
                            {activeTab === 'access' && (
                                <AccessControlTab
                                    groups={groups}
                                    policies={policies}
                                    users={users}
                                    departments={departments}
                                    topics={topics}
                                    onRefresh={fetchData}
                                    setMessage={setMessage}
                                />
                            )}
                            {activeTab === 'company' && <CompanyTab />}
                            {activeTab === 'features' && <FeatureAccessTab />}
                            {activeTab === 'priorities' && <PrioritiesTab />}
                            {activeTab === 'labels' && <LabelsTab />}
                            {activeTab === 'aicost' && <AICostTab />}
                            {activeTab === 'l10' && <L10MeetingsTab />}
                            {activeTab === 'looseends' && <LooseEndsTab />}
                            {activeTab === 'whatsnew' && <WhatsNewTab />}
                            {activeTab === 'chat' && <ChatModelTab />}
                            {activeTab === 'salesmodels' && <SalesModelsTab />}
                            {activeTab === 'folders' && <DepartmentFoldersSection />}
                            {activeTab === 'departments' && <Departments />}
                            {activeTab === 'feedback' && <FeedbackTab />}
                            {activeTab === 'logins' && <LoginActivityTab />}
                        </AnimatePresence>
                    )}
                    </div>
                </section>
            </div>

            {/* Add User Modal */}
            <Modal
                isOpen={showAddUserModal}
                onClose={() => setShowAddUserModal(false)}
                title="Register New User"
                size="lg"
            >
                {/* Same two-column split as Edit User: identity on the left, EOS
                    access on the right, so both forms read the same way. */}
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-4 items-start">
                    <div className="space-y-4">
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">Full Name</label>
                            <input
                                type="text"
                                className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                                value={newUser.name}
                                onChange={e => setNewUser({ ...newUser, name: e.target.value })}
                                placeholder="John Doe"
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">Email Address</label>
                            <input
                                type="email"
                                className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                                value={newUser.email}
                                onChange={e => setNewUser({ ...newUser, email: e.target.value })}
                                placeholder="john@example.com"
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">Department</label>
                            <select
                                className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                                value={newUser.department}
                                onChange={e => setNewUser({ ...newUser, department: e.target.value })}
                            >
                                <option value="general">general</option>
                                {departments.filter(d => d !== 'general').map(dept => (
                                    <option key={dept} value={dept}>{dept}</option>
                                ))}
                            </select>
                        </div>
                    </div>
                    <div className="space-y-4">
                        <label className="flex items-start gap-3 p-3 bg-brand-tint border border-brand-soft rounded-lg cursor-pointer">
                            <input
                                type="checkbox"
                                checked={newUser.is_exec}
                                onChange={e => setNewUser({
                                    ...newUser,
                                    is_exec: e.target.checked,
                                    // '*' (org-wide) is Exec-only — drop it if Exec is turned off.
                                    managed_departments: e.target.checked
                                        ? newUser.managed_departments
                                        : newUser.managed_departments.filter(d => d !== '*'),
                                })}
                                className="mt-0.5 rounded border-brand-soft text-brand focus:ring-brand/30"
                            />
                            <span className="text-sm">
                                <span className="font-semibold text-ink">Is part of the Exec team</span>
                                <span className="block text-[11px] text-brand-hover mt-0.5">
                                    Exec members edit the VTO, see the Leadership Scorecard &amp; join the Exec L10.
                                    Leave OFF for a people-manager who only runs their own team's L10 — use
                                    "Team Manager" below instead.
                                </span>
                            </span>
                        </label>
                        {/* Team Manager — departments this person leads. Available to
                            ANY user (decoupled from Exec): grants leadership over the
                            selected teams' EOS + their L10 scope, without Exec-team
                            access. The '*' org-wide wildcard stays Exec-only. */}
                        <div>
                            <label className="block text-sm font-medium text-gray-700 mb-1">
                                Team Manager — departments this person leads
                                <span className="block text-[11px] font-normal text-gray-500 mt-0.5">
                                    Grants leadership over each selected team's EOS — see &amp; manage its
                                    Rocks, Issues, To-Dos &amp; Scorecard and run its L10 — <strong>without</strong>{' '}
                                    Exec-team access. Leave empty for a regular member.
                                    {newUser.is_exec && ' Choose "All departments" for an org-wide exec like the CEO.'}
                                </span>
                            </label>
                            <select
                                multiple
                                className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none h-32"
                                value={newUser.managed_departments}
                                onChange={e =>
                                    setNewUser({
                                        ...newUser,
                                        managed_departments: Array.from(
                                            e.target.selectedOptions,
                                            o => o.value,
                                        ),
                                    })
                                }
                            >
                                {newUser.is_exec && (
                                    <option value="*">All departments (org-wide — sees everything)</option>
                                )}
                                {departments.filter(d => d !== 'general').map(dept => (
                                    <option key={dept} value={dept}>
                                        {dept}
                                    </option>
                                ))}
                            </select>
                            <p className="text-[11px] text-gray-400 mt-1">
                                Hold ⌘ / Ctrl to select multiple.
                            </p>
                        </div>
                    </div>
                </div>
                <div className="mt-8 flex flex-col sm:flex-row sm:justify-end gap-3">
                    <button
                        onClick={handleCreateUser}
                        disabled={savingUser}
                        className="bg-brand hover:bg-brand-hover text-white px-8 py-2 rounded-lg font-semibold transition-all disabled:opacity-60 disabled:cursor-not-allowed flex items-center justify-center gap-2 sm:order-2"
                    >
                        {savingUser && <div className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full" />}
                        {savingUser ? 'Creating…' : 'Create User'}
                    </button>
                    <button
                        onClick={() => setShowAddUserModal(false)}
                        disabled={savingUser}
                        className="bg-gray-100 hover:bg-gray-200 text-gray-700 px-8 py-2 rounded-lg font-semibold transition-all disabled:opacity-60 disabled:cursor-not-allowed sm:order-1"
                    >
                        Cancel
                    </button>
                </div>
            </Modal>

            {/* Offboard User Modal */}
            {offboardingUser && (
                <OffboardUserModal
                    user={offboardingUser}
                    users={users}
                    onClose={() => setOffboardingUser(null)}
                    onDone={async (msg) => {
                        setOffboardingUser(null);
                        setMessage({ type: 'success', text: msg });
                        await fetchData();
                    }}
                />
            )}

            {/* Edit User Modal */}
            <Modal
                isOpen={showEditUserModal}
                onClose={() => setShowEditUserModal(false)}
                title="Edit User"
                size="lg"
            >
                {editingUser && (
                    <>
                        {/* Two columns on wide screens — who they are on the left, what
                            they can reach in EOS on the right. Each new option used to
                            make one tall scroll taller; side by side keeps the whole
                            form on a single screen. */}
                        <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-4 items-start">
                            <div className="space-y-4">
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Full Name</label>
                                    <input
                                        type="text"
                                        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                                        value={editingUser.name}
                                        onChange={e => setEditingUser({ ...editingUser, name: e.target.value })}
                                    />
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Email Address</label>
                                    <input
                                        type="email"
                                        className="w-full px-4 py-2 rounded-lg border border-gray-200 bg-gray-50 cursor-not-allowed"
                                        value={editingUser.email}
                                        disabled
                                    />
                                    <p className="text-xs text-gray-500 mt-1">Email cannot be changed</p>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">Department</label>
                                    <select
                                        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                                        value={editingUser.department}
                                        onChange={e => setEditingUser({ ...editingUser, department: e.target.value })}
                                    >
                                        <option value="general">general</option>
                                        {departments.filter(d => d !== 'general').map(dept => (
                                            <option key={dept} value={dept}>{dept}</option>
                                        ))}
                                    </select>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-2">Roles</label>
                                    {/* Two per row so the role list stays short as roles are added. */}
                                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2 gap-x-4 gap-y-2">
                                        {roles.map(role => (
                                            <label key={role.name} className="flex items-center gap-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={editingUser.roles.includes(role.name)}
                                                    onChange={(e) => {
                                                        if (e.target.checked) {
                                                            setEditingUser({
                                                                ...editingUser,
                                                                roles: [...editingUser.roles, role.name]
                                                            });
                                                        } else {
                                                            setEditingUser({
                                                                ...editingUser,
                                                                roles: editingUser.roles.filter(r => r !== role.name)
                                                            });
                                                        }
                                                    }}
                                                    className="rounded border-gray-300 text-brand focus:ring-brand/50 flex-shrink-0"
                                                />
                                                <span className="text-sm text-gray-700">{role.name}</span>
                                                {role.description && (
                                                    <span className="text-xs text-gray-500 truncate">- {role.description}</span>
                                                )}
                                            </label>
                                        ))}
                                    </div>
                                </div>
                            </div>
                            <div className="space-y-4">
                                <label className="flex items-start gap-3 p-3 bg-brand-tint border border-brand-soft rounded-lg cursor-pointer">
                                    <input
                                        type="checkbox"
                                        checked={editingUser.is_exec ?? false}
                                        onChange={e => setEditingUser({
                                            ...editingUser,
                                            is_exec: e.target.checked,
                                            // '*' (org-wide) is Exec-only — drop it if Exec is turned off.
                                            managed_departments: e.target.checked
                                                ? (editingUser.managed_departments ?? [])
                                                : (editingUser.managed_departments ?? []).filter(d => d !== '*'),
                                        })}
                                        className="mt-0.5 rounded border-brand-soft text-brand focus:ring-brand/30"
                                    />
                                    <span className="text-sm">
                                        <span className="font-semibold text-ink">Is part of the Exec team</span>
                                        <span className="block text-[11px] text-brand-hover mt-0.5">
                                            Exec members edit the VTO, see the Leadership Scorecard &amp; join the
                                            Exec L10. Leave OFF for a people-manager who only runs their own
                                            team's L10 — use "Team Manager" below instead.
                                        </span>
                                    </span>
                                </label>
                                {/* Team Manager — departments this person leads. Available to
                                    ANY user (decoupled from Exec): grants leadership over the
                                    selected teams' EOS + their L10 scope, without Exec-team
                                    access. The '*' org-wide wildcard stays Exec-only. */}
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">
                                        Team Manager — departments this person leads
                                        <span className="block text-[11px] font-normal text-gray-500 mt-0.5">
                                            Grants leadership over each selected team's EOS — see &amp; manage its
                                            Rocks, Issues, To-Dos &amp; Scorecard and run its L10 —{' '}
                                            <strong>without</strong> Exec-team access. Leave empty for a regular member.
                                            {(editingUser.is_exec ?? false) && ' Choose "All departments" for an org-wide exec like the CEO.'}
                                        </span>
                                    </label>
                                    <select
                                        multiple
                                        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none h-32"
                                        value={editingUser.managed_departments ?? []}
                                        onChange={e =>
                                            setEditingUser({
                                                ...editingUser,
                                                managed_departments: Array.from(
                                                    e.target.selectedOptions,
                                                    o => o.value,
                                                ),
                                            })
                                        }
                                    >
                                        {(editingUser.is_exec ?? false) && (
                                            <option value="*">All departments (org-wide — sees everything)</option>
                                        )}
                                        {departments.filter(d => d !== 'general').map(dept => (
                                            <option key={dept} value={dept}>
                                                {dept}
                                            </option>
                                        ))}
                                    </select>
                                    <p className="text-[11px] text-gray-400 mt-1">
                                        Hold ⌘ / Ctrl to select multiple.
                                    </p>
                                </div>
                                <div>
                                    <label className="block text-sm font-medium text-gray-700 mb-1">
                                        Additional EOS teams
                                        <span className="block text-[11px] font-normal text-gray-500 mt-0.5">
                                            For someone who sits in more than one L10. Lets them SEE and FILE
                                            that team&apos;s Rocks, Issues, To-Dos &amp; Scorecard — so what they
                                            add outside a meeting can land on the right team&apos;s list. It does{' '}
                                            <strong>not</strong> let them edit other people&apos;s items, schedule
                                            that team&apos;s L10, or reach the Exec team. Only teams whose L10
                                            roster currently includes them can be granted, so a grant lapses on
                                            its own when they come off that calendar invite.
                                        </span>
                                    </label>
                                    {eosTeamsSaving ? (
                                        <p className="text-[11px] text-gray-400">Checking their L10s…</p>
                                    ) : (eosTeams ?? []).length === 0 ? (
                                        <p className="text-[11px] text-gray-400">
                                            Not on any other team&apos;s L10 roster — nothing to grant. If they
                                            should be, add them to that meeting&apos;s Google Calendar invite and
                                            re-import it from the Meetings page.
                                        </p>
                                    ) : (
                                        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2 gap-x-4 gap-y-1">
                                            {(eosTeams ?? []).map(t => (
                                                <label key={t.department} className="flex items-center gap-2 text-sm text-gray-700">
                                                    <input
                                                        type="checkbox"
                                                        checked={t.granted}
                                                        onChange={e =>
                                                            setEosTeams(
                                                                (eosTeams ?? []).map(x =>
                                                                    x.department === t.department
                                                                        ? { ...x, granted: e.target.checked }
                                                                        : x,
                                                                ),
                                                            )
                                                        }
                                                    />
                                                    {t.department}
                                                </label>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="mt-8 flex flex-col sm:flex-row sm:justify-end gap-3">
                            <button
                                onClick={handleEditUser}
                                disabled={savingUser}
                                className="bg-brand hover:bg-brand-hover text-white px-8 py-2 rounded-lg font-semibold transition-all disabled:opacity-60 disabled:cursor-not-allowed flex items-center justify-center gap-2 sm:order-2"
                            >
                                {savingUser && <div className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full" />}
                                {savingUser ? 'Saving…' : 'Save Changes'}
                            </button>
                            <button
                                onClick={() => setShowEditUserModal(false)}
                                disabled={savingUser}
                                className="bg-gray-100 hover:bg-gray-200 text-gray-700 px-8 py-2 rounded-lg font-semibold transition-all disabled:opacity-60 disabled:cursor-not-allowed sm:order-1"
                            >
                                Cancel
                            </button>
                        </div>
                    </>
                )}
            </Modal>

            {/* Add/Edit Role Modal */}
            <RoleModal
                isOpen={showAddRoleModal || showEditRoleModal}
                onClose={() => {
                    setShowAddRoleModal(false);
                    setShowEditRoleModal(false);
                    setEditingRole(null);
                }}
                role={editingRole || newRole}
                setRole={editingRole ? setEditingRole : setNewRole}
                onSave={editingRole ? handleUpdateRole : handleCreateRole}
                isEditing={!!editingRole}
            />

            <div className="mt-8 p-6 bg-brand-tint rounded-2xl border border-red-100">
                <div className="flex items-start gap-4">
                    <div className="bg-brand p-2 rounded-lg text-white">
                        <Settings className="h-5 w-5" />
                    </div>
                    <div>
                        <h4 className="font-bold text-brand">Workspace Integration Note</h4>
                        <p className="text-brand-hover text-sm mt-1">
                            To enable Google Groups sync, you must authorize the Service Account in your Google Workspace Admin Console using Domain-Wide Delegation.
                            Ensure the scope <code className="bg-brand/10 px-1 rounded">admin.directory.group.readonly</code> is granted.
                        </p>
                    </div>
                </div>
            </div>
        </div>
    );
};

// Component: Modal
// `size` widens the card for forms that would otherwise be one very long
// scroll — 'md' stays the default so the shorter modals are unchanged.
const Modal: React.FC<{
    isOpen: boolean;
    onClose: () => void;
    title: string;
    size?: 'md' | 'lg';
    children: React.ReactNode;
}> = ({ isOpen, onClose, title, size = 'md', children }) => {
    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
            <motion.div
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                // Cap the card at the viewport height and let the body scroll, so
                // the action buttons stay reachable on short screens instead of
                // overflowing off the bottom with no scrollbar.
                className={`bg-white rounded-2xl w-full ${size === 'lg' ? 'max-w-4xl' : 'max-w-md'} shadow-2xl flex flex-col max-h-[90vh]`}
            >
                <div className="flex items-center justify-between p-6 pb-4 flex-shrink-0">
                    <h2 className="text-xl font-bold">{title}</h2>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
                        <X className="h-5 w-5" />
                    </button>
                </div>
                <div className="px-6 pb-6 overflow-y-auto">
                    {children}
                </div>
            </motion.div>
        </div>
    );
};

// Component: Role Modal with Permissions
const RoleModal: React.FC<{
    isOpen: boolean;
    onClose: () => void;
    role: Role;
    setRole: (role: Role) => void;
    onSave: () => void;
    isEditing: boolean;
}> = ({ isOpen, onClose, role, setRole, onSave, isEditing }) => {
    if (!isOpen) return null;

    const togglePermission = (resource: string, permission: string) => {
        setRole({
            ...role,
            permissions: {
                ...role.permissions,
                [resource]: {
                    ...role.permissions[resource],
                    [permission]: !role.permissions[resource]?.[permission as keyof ResourcePermissions]
                }
            }
        });
    };

    return (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
            <motion.div
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="bg-white rounded-2xl p-6 w-full max-w-2xl shadow-2xl max-h-[90vh] overflow-y-auto"
            >
                <div className="flex items-center justify-between mb-6">
                    <h2 className="text-xl font-bold">{isEditing ? 'Edit Role' : 'Create New Role'}</h2>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
                        <X className="h-5 w-5" />
                    </button>
                </div>

                <div className="space-y-6">
                    <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">Role Name</label>
                        <input
                            type="text"
                            className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none disabled:bg-gray-50 disabled:cursor-not-allowed"
                            value={role.name}
                            onChange={e => setRole({ ...role, name: e.target.value })}
                            placeholder="content_manager"
                            disabled={isEditing}
                        />
                        {isEditing && <p className="text-xs text-gray-500 mt-1">Role name cannot be changed</p>}
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-gray-700 mb-1">Description</label>
                        <textarea
                            className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                            value={role.description}
                            onChange={e => setRole({ ...role, description: e.target.value })}
                            placeholder="Describe what this role can do..."
                            rows={2}
                        />
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-gray-700 mb-3">Permissions</label>
                        <div className="space-y-4">
                            {RESOURCE_TYPES.map(resource => (
                                <div key={resource} className="border border-gray-200 rounded-lg p-4">
                                    <h4 className="font-semibold text-gray-900 mb-3 capitalize">{resource}</h4>
                                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                                        {PERMISSION_TYPES.map(permission => (
                                            <label key={permission} className="flex items-center gap-2 cursor-pointer">
                                                <input
                                                    type="checkbox"
                                                    checked={role.permissions[resource]?.[permission as keyof ResourcePermissions] || false}
                                                    onChange={() => togglePermission(resource, permission)}
                                                    className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                                                />
                                                <span className="text-sm text-gray-700 capitalize">{permission}</span>
                                            </label>
                                        ))}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                <div className="mt-8 flex gap-3">
                    <button
                        onClick={onSave}
                        className="flex-1 bg-brand hover:bg-brand-hover text-white py-2 rounded-lg font-semibold transition-all"
                    >
                        {isEditing ? 'Save Changes' : 'Create Role'}
                    </button>
                    <button
                        onClick={onClose}
                        className="flex-1 bg-gray-100 hover:bg-gray-200 text-gray-700 py-2 rounded-lg font-semibold transition-all"
                    >
                        Cancel
                    </button>
                </div>
            </motion.div>
        </div>
    );
};

// Component: Login Activity Tab
interface LoginEvent {
    id: number;
    user_email: string;
    timestamp: string;
    ip_address?: string | null;
    user_agent?: string | null;
}

// Backend stores naive UTC timestamps (no tz suffix). `formatInstant` reads
// them as UTC so the browser renders them in the admin's own zone — this used
// to carry its own copy of that rule; the rule now lives in one place.
const fmtLoginTime = (ts: string): string => formatInstant(ts, {}, ts || '—');

// Best-effort, human-friendly device label from a raw User-Agent string.
const prettyUserAgent = (ua?: string | null): string => {
    if (!ua) return 'Unknown';
    const os = /Windows/.test(ua) ? 'Windows'
        : /Mac OS X|Macintosh/.test(ua) ? 'macOS'
        : /iPhone|iPad|iOS/.test(ua) ? 'iOS'
        : /Android/.test(ua) ? 'Android'
        : /Linux/.test(ua) ? 'Linux' : '';
    const browser = /Edg\//.test(ua) ? 'Edge'
        : /OPR\/|Opera/.test(ua) ? 'Opera'
        : /Chrome\//.test(ua) ? 'Chrome'
        : /Firefox\//.test(ua) ? 'Firefox'
        : /Safari\//.test(ua) ? 'Safari' : '';
    const label = [browser, os].filter(Boolean).join(' · ');
    return label || ua.slice(0, 40);
};

const LoginActivityTab: React.FC = () => {
    const [events, setEvents] = useState<LoginEvent[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [search, setSearch] = useState('');

    useEffect(() => {
        let active = true;
        (async () => {
            setLoading(true);
            try {
                const res = await client.get('/api/admin/logins', { params: { limit: 1000 } });
                if (active) setEvents(res.data);
            } catch (err) {
                console.error('Failed to load login activity:', err);
                if (active) setError('Failed to load login activity.');
            } finally {
                if (active) setLoading(false);
            }
        })();
        return () => { active = false; };
    }, []);

    const filtered = events.filter(e =>
        e.user_email.toLowerCase().includes(search.toLowerCase())
        || (e.ip_address ?? '').toLowerCase().includes(search.toLowerCase())
    );

    const uniqueUsers = new Set(filtered.map(e => e.user_email)).size;

    const exportCsv = () => {
        const header = ['Email', 'Timestamp (UTC)', 'IP Address', 'User Agent'];
        const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`;
        const lines = [
            header.map(esc).join(','),
            ...filtered.map(e => [e.user_email, e.timestamp, e.ip_address ?? '', e.user_agent ?? ''].map(esc).join(',')),
        ];
        const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'login-activity.csv';
        a.click();
        URL.revokeObjectURL(url);
    };

    return (
        <motion.div
            key="logins"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 10 }}
        >
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-2">
                <div>
                    <h3 className="text-lg font-semibold text-gray-900">Login Activity</h3>
                    <p className="text-sm text-gray-500">Who signed in and when. Each row is one login session.</p>
                </div>
                <div className="flex gap-3 items-center">
                    <div className="relative w-56">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
                        <input
                            type="text"
                            placeholder="Filter by email or IP"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            className="w-full pl-10 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-1 focus:ring-brand"
                        />
                    </div>
                    <button
                        onClick={exportCsv}
                        disabled={filtered.length === 0}
                        className="flex items-center gap-2 bg-brand hover:bg-brand-hover disabled:opacity-50 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
                    >
                        <Download className="h-4 w-4" />
                        Export CSV
                    </button>
                </div>
            </div>

            <div className="flex gap-4 items-center mb-4 text-sm text-gray-500">
                <span><span className="font-semibold text-gray-900">{filtered.length}</span> logins</span>
                <span><span className="font-semibold text-gray-900">{uniqueUsers}</span> distinct users</span>
            </div>

            {loading ? (
                <div className="flex items-center justify-center py-12">
                    <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand"></div>
                </div>
            ) : error ? (
                <div className="bg-red-50 text-red-700 p-4 rounded-lg flex items-center gap-3">
                    <AlertCircle className="h-5 w-5" /> {error}
                </div>
            ) : (
                <div className="bg-white border border-gray-100 rounded-lg max-h-[60vh] overflow-y-auto">
                    <table className="w-full text-left relative">
                        <thead className="sticky top-0 bg-gray-50/95 backdrop-blur z-10 shadow-sm">
                            <tr className="text-gray-500 text-sm border-b border-gray-100">
                                <th className="py-3 px-4 font-semibold">User</th>
                                <th className="py-3 px-4 font-semibold">When</th>
                                <th className="py-3 px-4 font-semibold">IP Address</th>
                                <th className="py-3 px-4 font-semibold">Device</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-50">
                            {filtered.map(e => (
                                <tr key={e.id} className="hover:bg-gray-50/50 transition-colors">
                                    <td className="py-3 px-4 text-sm font-medium text-gray-900">{e.user_email}</td>
                                    <td className="py-3 px-4 text-sm text-gray-600">{fmtLoginTime(e.timestamp)}</td>
                                    <td className="py-3 px-4 text-sm text-gray-600 font-mono text-xs">{e.ip_address || '—'}</td>
                                    <td className="py-3 px-4 text-sm text-gray-600" title={e.user_agent || undefined}>{prettyUserAgent(e.user_agent)}</td>
                                </tr>
                            ))}
                            {filtered.length === 0 && (
                                <tr>
                                    <td colSpan={4} className="py-10 text-center text-gray-400 italic">
                                        No login activity recorded yet.
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            )}
        </motion.div>
    );
};

// Component: Offboard User Modal — successor pick + what-will-happen + confirm.
const OffboardUserModal: React.FC<{
    user: UserProfile;
    users: UserProfile[];
    onClose: () => void;
    onDone: (message: string) => Promise<void> | void;
}> = ({ user, users, onClose, onDone }) => {
    const [successor, setSuccessor] = useState('');
    const [suggestion, setSuggestion] = useState<{ email: string; name: string } | null>(null);
    const [confirmText, setConfirmText] = useState('');
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Prefill with the person's EOS-chart boss when the chart knows one.
    useEffect(() => {
        let cancelled = false;
        client
            .get(`/api/offboarding/suggest-successor`, { params: { email: user.email } })
            .then(res => {
                if (cancelled) return;
                const sug = res.data?.suggestion ?? null;
                setSuggestion(sug);
                if (sug?.email) setSuccessor(prev => prev || sug.email);
            })
            .catch(() => { /* suggestion is a nicety — the picker still works */ });
        return () => { cancelled = true; };
    }, [user.email]);

    const candidates = users
        .filter(u => u.email.toLowerCase() !== user.email.toLowerCase())
        .sort((a, b) => (a.name || a.email).localeCompare(b.name || b.email));

    const submit = async () => {
        setSubmitting(true);
        setError(null);
        try {
            const res = await client.post('/api/offboarding/offboard', {
                email: user.email,
                successor_email: successor,
                run_sweep: true,
            });
            const executor = res.data?.sweep_executor;
            const reassigned = res.data?.assets_reassigned ?? 0;
            await onDone(
                `${user.name || user.email} offboarded — ${reassigned} doc(s) moved to ${successor}; ` +
                (executor === 'skipped' ? 'no handoff sweep was run.' : 'the handoff sweep is running, the dossier appears on the successor\u2019s Handoffs page shortly.')
            );
        } catch (err: unknown) {
            const e = err as { response?: { data?: { detail?: string } } };
            setError(e.response?.data?.detail ?? 'Offboarding failed — nothing was changed.');
            setSubmitting(false);
        }
    };

    return (
        <Modal isOpen onClose={onClose} title={`Offboard ${user.name || user.email}`}>
            <div className="space-y-4">
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                    Run this <span className="font-semibold">before</span> IT suspends their Google
                    account — Cubie takes one last pass over their email and calendar to capture open
                    commitments, which stops working the moment the account is suspended.
                </div>
                <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Successor (inherits their open work)</label>
                    <select
                        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none bg-white"
                        value={successor}
                        onChange={e => setSuccessor(e.target.value)}
                    >
                        <option value="">Select a successor…</option>
                        {candidates.map(u => (
                            <option key={u.email} value={u.email}>
                                {u.name || u.email} ({u.department || 'general'})
                                {suggestion?.email === u.email.toLowerCase() ? ' — their manager' : ''}
                            </option>
                        ))}
                    </select>
                    {suggestion && (
                        <p className="mt-1 text-xs text-gray-500">
                            Suggested from the Accountability Chart: {suggestion.name}
                        </p>
                    )}
                </div>
                <ul className="text-sm text-gray-600 list-disc pl-5 space-y-1">
                    <li>Their sign-in and daily AI Priorities stop immediately.</li>
                    <li>Their uploaded docs are reassigned to the successor.</li>
                    <li>Cubie sweeps everything they left open — EOS to-dos, rocks, L10s they ran, HubSpot deals, commitments in email — into a handoff dossier.</li>
                    <li>Open items keep appearing in the successor&rsquo;s daily priorities until each is closed.</li>
                    <li>The successor (and org-wide executives) can still read their past priorities, read-only and audit-logged.</li>
                </ul>
                <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                        Type <span className="font-mono">{user.email}</span> to confirm
                    </label>
                    <input
                        type="text"
                        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-blue-500 outline-none"
                        value={confirmText}
                        onChange={e => setConfirmText(e.target.value)}
                        placeholder={user.email}
                    />
                </div>
                {error && <div className="text-sm text-red-600">{error}</div>}
                <div className="flex gap-3 pt-1">
                    <button
                        onClick={submit}
                        disabled={submitting || !successor || confirmText.trim().toLowerCase() !== user.email.toLowerCase()}
                        className="flex-1 bg-amber-600 hover:bg-amber-700 text-white py-2 rounded-lg font-semibold transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                    >
                        {submitting && <div className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full" />}
                        {submitting ? 'Offboarding…' : 'Offboard & hand off'}
                    </button>
                    <button
                        onClick={onClose}
                        disabled={submitting}
                        className="flex-1 bg-gray-100 hover:bg-gray-200 text-gray-700 py-2 rounded-lg font-semibold transition-all disabled:opacity-60"
                    >
                        Cancel
                    </button>
                </div>
            </div>
        </Modal>
    );
};

// Component: Users Tab
const UsersTab: React.FC<{
    users: UserProfile[];
    roles: Role[];
    onAddUser: () => void;
    onEditUser: (user: UserProfile) => void;
    onOffboardUser: (user: UserProfile) => void;
    onDeleteUser: (email: string) => void;
}> = ({ users, roles, onAddUser, onEditUser, onOffboardUser, onDeleteUser }) => {
    const [search, setSearch] = useState('');
    const [openMenu, setOpenMenu] = useState<string | null>(null);

    // ── Filters popover ─────────────────────────────────────────────
    // The list is filterable by the dimensions that actually exist on a
    // user record and map to the visible columns: Role, Organization
    // (department) and Access (Exec team / Team Manager). Within a group
    // selections are OR'd; across groups they're AND'd. Options are derived
    // from the loaded users so a facet is never shown that matches nobody.
    // (There is deliberately no "Status" facet — active/inactive is not a
    // real user field yet; the count pills + Status column are placeholders.)
    const [filterOpen, setFilterOpen] = useState(false);
    const [roleFilter, setRoleFilter] = useState<string[]>([]);
    const [deptFilter, setDeptFilter] = useState<string[]>([]);
    const [accessFilter, setAccessFilter] = useState<string[]>([]);
    const filterRef = useRef<HTMLDivElement>(null);

    const roleOptions = useMemo(() => {
        const s = new Set<string>();
        users.forEach(u => (u.roles || []).forEach(r => s.add(r)));
        return Array.from(s).sort((a, b) => a.localeCompare(b));
    }, [users]);

    const deptOptions = useMemo(() => {
        const s = new Set<string>();
        users.forEach(u => s.add(u.department || 'general'));
        return Array.from(s).sort((a, b) => a.localeCompare(b));
    }, [users]);

    // Mirror the "Manager" badge rule in the table so filter results match
    // the badges the admin sees.
    const isManager = (u: UserProfile) =>
        (u.managed_departments ?? []).filter(d => d && d !== '*').length > 0;

    const visibleUsers = useMemo(() => {
        const q = search.trim().toLowerCase();
        return users
            .filter(u => !q || u.name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q))
            .filter(u => roleFilter.length === 0 || (u.roles || []).some(r => roleFilter.includes(r)))
            .filter(u => deptFilter.length === 0 || deptFilter.includes(u.department || 'general'))
            .filter(u => {
                if (accessFilter.length === 0) return true;
                return (accessFilter.includes('exec') && !!u.is_exec)
                    || (accessFilter.includes('manager') && isManager(u));
            })
            .sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
    }, [users, search, roleFilter, deptFilter, accessFilter]);

    const activeFilterCount = roleFilter.length + deptFilter.length + accessFilter.length;
    const clearFilters = () => { setRoleFilter([]); setDeptFilter([]); setAccessFilter([]); };

    // Close the popover on outside-click or Escape (matches TopBar.tsx).
    useEffect(() => {
        if (!filterOpen) return;
        const onDocClick = (e: MouseEvent) => {
            if (filterRef.current && !filterRef.current.contains(e.target as Node)) setFilterOpen(false);
        };
        const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setFilterOpen(false); };
        document.addEventListener('mousedown', onDocClick);
        document.addEventListener('keydown', onEsc);
        return () => {
            document.removeEventListener('mousedown', onDocClick);
            document.removeEventListener('keydown', onEsc);
        };
    }, [filterOpen]);

    const renderCheckGroup = (
        title: string,
        options: { value: string; label: string }[],
        selected: string[],
        setter: React.Dispatch<React.SetStateAction<string[]>>,
    ) => {
        if (options.length === 0) return null;
        return (
            <div>
                <div className="text-[11px] uppercase tracking-wider font-semibold text-gray-400 mb-2">{title}</div>
                <div className="space-y-1.5">
                    {options.map(o => (
                        <label key={o.value} className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                            <input
                                type="checkbox"
                                checked={selected.includes(o.value)}
                                onChange={() => setter(prev => prev.includes(o.value) ? prev.filter(v => v !== o.value) : [...prev, o.value])}
                                className="rounded border-gray-300 text-brand focus:ring-brand"
                            />
                            {o.label}
                        </label>
                    ))}
                </div>
            </div>
        );
    };

    const getRoleStyle = (roleName: string) => {
        const name = roleName.toLowerCase();
        if (name.includes('client admin')) return 'bg-blue-50 text-blue-600 border border-blue-200';
        if (name.includes('admin')) return 'bg-brand-tint text-brand border border-brand-soft';
        if (name.includes('client user')) return 'bg-orange-50 text-orange-600 border border-orange-200';
        return 'bg-gray-50 text-gray-600 border border-gray-200';
    };

    const getAvatarInitials = (name: string) => {
        if (!name) return 'U';
        const parts = name.split(' ');
        if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
        return name.slice(0, 2).toUpperCase();
    };

    return (
        <motion.div
            key="users"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 10 }}
        >
            {/* Top Bar with counts, search, and add user */}
            <div className="flex justify-between items-center mb-6">
                <div className="flex gap-4 items-center">
                    <div className="bg-brand text-white px-4 py-1.5 rounded text-sm font-semibold shadow-sm flex items-center">
                        <span className="mr-2">14 Active</span>
                    </div>
                    <div className="bg-[#1e3a5f] text-white px-4 py-1.5 rounded text-sm font-semibold shadow-sm flex items-center">
                        <span className="mr-2">2 Inactive</span>
                    </div>
                </div>
            </div>

            <div className="flex justify-between items-center mb-6">
                <div className="flex gap-4 items-center flex-1">
                    <div className="relative w-64">
                        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
                        <input
                            type="text"
                            placeholder="Search"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            className="w-full pl-10 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-1 focus:ring-brand"
                        />
                    </div>
                    <div className="relative" ref={filterRef}>
                        <button
                            onClick={() => setFilterOpen(o => !o)}
                            className={`flex items-center gap-2 border px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                                activeFilterCount > 0 || filterOpen
                                    ? 'border-brand bg-brand/5 text-brand'
                                    : 'border-gray-200 bg-white text-gray-700 hover:bg-gray-50'
                            }`}
                        >
                            <Filter className="h-4 w-4" />
                            Filters
                            {activeFilterCount > 0 && (
                                <span className="ml-1 inline-flex items-center justify-center h-5 min-w-[1.25rem] px-1 rounded-full bg-brand text-white text-[11px] font-semibold">
                                    {activeFilterCount}
                                </span>
                            )}
                        </button>

                        <AnimatePresence>
                            {filterOpen && (
                                <motion.div
                                    initial={{ opacity: 0, y: -4 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: -4 }}
                                    transition={{ duration: 0.12 }}
                                    className="absolute left-0 top-full mt-2 w-72 bg-white rounded-xl shadow-xl border border-gray-200 z-50 overflow-hidden"
                                >
                                    <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
                                        <span className="text-sm font-semibold text-gray-900">Filters</span>
                                        {activeFilterCount > 0 && (
                                            <button onClick={clearFilters} className="text-xs font-medium text-brand hover:underline">
                                                Clear all
                                            </button>
                                        )}
                                    </div>
                                    <div className="max-h-80 overflow-y-auto px-4 py-3 space-y-4">
                                        {renderCheckGroup('Role', roleOptions.map(r => ({ value: r, label: r })), roleFilter, setRoleFilter)}
                                        {renderCheckGroup('Organization', deptOptions.map(d => ({ value: d, label: d === 'general' ? 'All organizations' : d })), deptFilter, setDeptFilter)}
                                        {renderCheckGroup('Access', [{ value: 'exec', label: 'Exec team' }, { value: 'manager', label: 'Team Manager' }], accessFilter, setAccessFilter)}
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>
                    </div>
                </div>

                <button
                    onClick={onAddUser}
                    className="bg-brand hover:bg-brand-hover text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
                >
                    <Plus className="h-4 w-4" />
                    Add User
                </button>
            </div>

            <div className="bg-white border border-gray-100 rounded-lg max-h-[60vh] overflow-y-auto">
                <table className="w-full text-left relative">
                    <thead className="sticky top-0 bg-gray-50/95 backdrop-blur z-10 shadow-sm">
                        <tr className="text-gray-500 text-sm border-b border-gray-100">
                            <th className="py-3 px-4 font-semibold w-1/4">User</th>
                            <th className="py-3 px-4 font-semibold w-1/5">Role</th>
                            <th className="py-3 px-4 font-semibold w-1/4">Organizations</th>
                            <th className="py-3 px-4 font-semibold w-32">Status</th>
                            <th className="py-3 px-4 font-semibold text-center w-24">Action</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                        {visibleUsers.map(user => (
                            <tr key={user.email} className="group hover:bg-gray-50/50 transition-colors">
                                <td className="py-3 px-4">
                                    <div className="flex items-center gap-3">
                                        <div className="h-9 w-9 bg-[#1e3a5f] rounded-full flex items-center justify-center text-white text-sm font-bold shadow-sm">
                                            {getAvatarInitials(user.name)}
                                        </div>
                                        <div className="flex flex-col">
                                            <span className="font-semibold text-sm text-gray-900">{user.name}</span>
                                            <span className="text-xs text-gray-500">{user.email}</span>
                                        </div>
                                    </div>
                                </td>
                                <td className="py-3 px-4">
                                    <div className="flex flex-wrap gap-1">
                                        {user.roles.length > 0 ? user.roles.map(roleName => {
                                            const role = roles.find(r => r.name === roleName);
                                            return (
                                                <span
                                                    key={roleName}
                                                    className={`px-2 py-0.5 text-xs font-semibold rounded-full flex items-center gap-1.5 ${getRoleStyle(roleName)}`}
                                                    title={role?.description}
                                                >
                                                    <User className="h-3 w-3" />
                                                    {roleName}
                                                </span>
                                            );
                                        }) : (
                                            <span className="px-2 py-0.5 bg-gray-50 text-gray-600 border border-gray-200 text-xs font-semibold rounded-full flex items-center gap-1.5">
                                                <User className="h-3 w-3" />
                                                No Role
                                            </span>
                                        )}
                                    </div>
                                </td>
                                <td className="py-3 px-4">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <span className="text-sm text-gray-600">
                                            {user.department === 'general' ? 'All organizations' : user.department}
                                        </span>
                                        {user.is_exec && (
                                            <span className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-brand-soft/60 text-brand-hover">
                                                Exec
                                            </span>
                                        )}
                                        {(user.managed_departments ?? []).filter(d => d && d !== '*').length > 0 && (
                                            <span
                                                className="text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded-full bg-teal-100 text-teal-700"
                                                title={`Team Manager: ${(user.managed_departments ?? []).filter(d => d && d !== '*').join(', ')}`}
                                            >
                                                Manager
                                            </span>
                                        )}
                                    </div>
                                </td>
                                <td className="py-3 px-4">
                                    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border border-green-200 text-green-700 bg-green-50">
                                        Active
                                    </span>
                                </td>
                                <td className="py-3 px-4 text-center">
                                    <div className="relative inline-block text-left">
                                        <button
                                            onClick={() => setOpenMenu(openMenu === user.email ? null : user.email)}
                                            className="text-gray-400 hover:text-gray-600 transition-colors p-1 rounded hover:bg-gray-100"
                                        >
                                            <MoreHorizontal className="h-5 w-5" />
                                        </button>

                                        {openMenu === user.email && (
                                            <div className="origin-top-right absolute right-0 mt-2 w-40 rounded-md shadow-lg bg-white ring-1 ring-black ring-opacity-5 z-10">
                                                <div className="py-1">
                                                    <button
                                                        onClick={() => { setOpenMenu(null); onEditUser(user); }}
                                                        className="w-full text-left flex items-center px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
                                                    >
                                                        <Edit className="mr-2 h-4 w-4" /> Edit
                                                    </button>
                                                    <button
                                                        onClick={() => { setOpenMenu(null); onOffboardUser(user); }}
                                                        className="w-full text-left flex items-center px-4 py-2 text-sm text-amber-700 hover:bg-amber-50"
                                                    >
                                                        <UserMinus className="mr-2 h-4 w-4" /> Offboard…
                                                    </button>
                                                    <button
                                                        onClick={() => { setOpenMenu(null); onDeleteUser(user.email); }}
                                                        className="w-full text-left flex items-center px-4 py-2 text-sm text-red-600 hover:bg-red-50"
                                                    >
                                                        <Trash2 className="mr-2 h-4 w-4" /> Delete
                                                    </button>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                </td>
                            </tr>
                        ))}
                        {visibleUsers.length === 0 && (
                            <tr>
                                <td colSpan={5} className="py-10 text-center text-gray-400 italic">
                                    {users.length === 0
                                        ? 'No users yet.'
                                        : 'No users match your search or filters.'}
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </motion.div>
    );
};

// Component: Roles Tab
const RolesTab: React.FC<{
    roles: Role[];
    onAddRole: () => void;
    onEditRole: (role: Role) => void;
    onDeleteRole: (roleName: string) => void;
}> = ({ roles, onAddRole, onEditRole, onDeleteRole }) => {
    return (
        <motion.div
            key="roles"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            className="space-y-6"
        >
            <div className="flex justify-between items-center">
                <h3 className="text-lg font-semibold text-gray-900">System Roles</h3>
                <button
                    onClick={onAddRole}
                    className="bg-brand hover:bg-brand-hover text-white px-4 py-2 rounded-lg font-medium transition-all flex items-center gap-2"
                >
                    <Plus className="h-4 w-4" />
                    Add Role
                </button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {roles.map(role => (
                    <div key={role.name} className="flex flex-col p-4 bg-gray-50 rounded-xl border border-gray-100 group hover:border-brand/30 transition-all">
                        <div className="flex items-start justify-between mb-3">
                            <div className="flex items-center gap-3">
                                <div className="bg-brand-tint p-2 rounded-lg text-brand">
                                    <Shield className="h-4 w-4" />
                                </div>
                                <div>
                                    <span className="font-medium text-gray-900 block">{role.name}</span>
                                    {role.description && (
                                        <span className="text-sm text-gray-500">{role.description}</span>
                                    )}
                                </div>
                            </div>
                            <div className="flex gap-1">
                                <button
                                    onClick={() => onEditRole(role)}
                                    className="text-gray-400 hover:text-brand opacity-0 group-hover:opacity-100 transition-all p-1"
                                    title="Edit role"
                                >
                                    <Edit className="h-4 w-4" />
                                </button>
                                <button
                                    onClick={() => onDeleteRole(role.name)}
                                    className="text-gray-400 hover:text-red-600 opacity-0 group-hover:opacity-100 transition-all p-1"
                                    title="Delete role"
                                >
                                    <Trash2 className="h-4 w-4" />
                                </button>
                            </div>
                        </div>
                        <div className="flex flex-wrap gap-1 mt-2">
                            {Object.entries(role.permissions).map(([resource, perms]) => {
                                const activePerms = Object.entries(perms)
                                    .filter(([_, active]) => active)
                                    .map(([perm]) => perm[0].toUpperCase());
                                if (activePerms.length === 0) return null;
                                return (
                                    <span
                                        key={resource}
                                        className="px-2 py-1 bg-white text-gray-600 text-xs rounded border border-gray-200"
                                        title={`${resource}: ${activePerms.join(', ')}`}
                                    >
                                        {resource.slice(0, 3)}: {activePerms.join('')}
                                    </span>
                                );
                            })}
                        </div>
                    </div>
                ))}
            </div>
        </motion.div>
    );
};

// Component: Workspace Tab
const WorkspaceTab: React.FC<{
    workspaceConfig: WorkspaceConfig | null;
    workspaceIdentity: WorkspaceIdentity | null;
    workspaceSlug: string | null;
    setWorkspaceConfig: React.Dispatch<React.SetStateAction<WorkspaceConfig | null>>;
    workspaceGroups: { email: string, name: string }[];
    roles: Role[];
    saving: boolean;
    onSave: () => void;
}> = ({ workspaceConfig, workspaceIdentity, workspaceSlug, setWorkspaceConfig, workspaceGroups, roles, saving, onSave }) => {
    const [isAddingGroup, setIsAddingGroup] = useState(false);
    const [newGroupEmail, setNewGroupEmail] = useState('');
    const [addingRoleToGroup, setAddingRoleToGroup] = useState<string | null>(null);
    const additionalDomains = workspaceIdentity?.domains.filter(
        domain => domain !== workspaceIdentity.primary_domain,
    ) ?? [];

    const handleAddGroup = () => {
        if (newGroupEmail && workspaceConfig) {
            setWorkspaceConfig({
                ...workspaceConfig,
                mappings: { ...workspaceConfig.mappings, [newGroupEmail]: [] }
            });
            setNewGroupEmail('');
            setIsAddingGroup(false);
        }
    };

    return (
        <motion.div
            key="workspace"
            initial={{ opacity: 0, x: 10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -10 }}
            className="space-y-8"
        >
            <section data-tour="admin-workspace-modules" className="max-w-2xl rounded-xl border border-gray-200 bg-white p-5 space-y-4">
                <div>
                    <h3 className="text-lg font-semibold text-gray-900">Workspace access</h3>
                    <p className="text-sm text-gray-500">Your company domains and available areas are managed by Cubie platform support.</p>
                </div>
                {workspaceIdentity ? (
                    <>
                        <div className="space-y-1">
                            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Primary sign-in domain</p>
                            <p className="font-medium text-gray-900">{workspaceIdentity.primary_domain || 'Not configured'}</p>
                            {additionalDomains.length > 0 && (
                                <p className="text-sm text-gray-600">Also accepted: {additionalDomains.join(', ')}</p>
                            )}
                        </div>
                        <div className="flex flex-wrap gap-2" aria-label="Available workspace areas">
                            {([
                                ['core', 'Core Cubie'],
                                ['eos', 'EOS'],
                                ['sales_coach', 'Sales Coach'],
                            ] as const).map(([key, label]) => (
                                <span key={key} className={cn('rounded-full px-3 py-1 text-xs font-medium', workspaceIdentity.modules[key] ? 'bg-green-50 text-green-800' : 'bg-gray-100 text-gray-600')}>
                                    {label}: {workspaceIdentity.modules[key] ? 'Available' : 'Not enabled'}
                                </span>
                            ))}
                        </div>
                    </>
                ) : (
                    <p className="text-sm text-gray-500">Workspace access details are unavailable right now.</p>
                )}
            </section>
            <HubSpotSettingsCard workspaceSlug={workspaceSlug} />
            <WorkspaceOperationsCard workspaceSlug={workspaceSlug} />
            <WorkspaceAIProvidersCard key={workspaceSlug ?? ''} workspaceSlug={workspaceSlug} />
            <section data-tour="admin-workspace-multi-gmail" className="max-w-2xl rounded-xl border border-gray-200 bg-white p-5">
                <div className="flex items-center justify-between gap-4">
                    <div>
                        <h3 className="text-base font-semibold text-gray-900">Additional Gmail accounts</h3>
                        <p className="mt-1 text-sm text-gray-600">Allow people to connect a personal or second work Gmail account. Their work inbox stays the default, and they choose another mailbox in Productivity → Email. Connected mailboxes are included automatically in each owner&apos;s AI Priorities and scheduled summaries, labeled by address. Personal mail stays out of team and manager views.</p>
                    </div>
                    <button
                        type="button"
                        role="switch"
                        aria-label="Allow additional Gmail accounts"
                        aria-checked={Boolean(workspaceConfig?.settings.multi_gmail_enabled)}
                        onClick={() => setWorkspaceConfig(prev => prev ? {
                            ...prev,
                            settings: { ...prev.settings, multi_gmail_enabled: !prev.settings.multi_gmail_enabled },
                        } : null)}
                        className={cn(
                            'relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors',
                            workspaceConfig?.settings.multi_gmail_enabled ? 'bg-brand' : 'bg-gray-200',
                        )}
                    >
                        <span className={cn(
                            'inline-block h-4 w-4 transform rounded-full bg-white transition-transform',
                            workspaceConfig?.settings.multi_gmail_enabled ? 'translate-x-6' : 'translate-x-1',
                        )} />
                    </button>
                </div>
                <p className="mt-3 text-xs text-gray-500">Save Settings below to apply this workspace policy. Each user connects and removes their own accounts in Settings → Email.</p>
            </section>
            {/* Settings Section */}
            <div className="max-w-2xl space-y-6">
                <div className="flex items-center justify-between">
                    <div>
                        <h3 className="text-lg font-semibold text-gray-900">Google Workspace Sync</h3>
                        <p className="text-sm text-gray-500">Automatically sync user roles from Google Groups.</p>
                    </div>
                    <button
                        onClick={() => setWorkspaceConfig(prev => prev ? {
                            ...prev,
                            settings: { ...prev.settings, workspace_sync_enabled: !prev.settings.workspace_sync_enabled }
                        } : null)}
                        className={cn(
                            "relative inline-flex h-6 w-11 items-center rounded-full transition-colors",
                            workspaceConfig?.settings.workspace_sync_enabled ? "bg-brand" : "bg-gray-200"
                        )}
                    >
                        <span className={cn(
                            "inline-block h-4 w-4 transform rounded-full bg-white transition-transform",
                            workspaceConfig?.settings.workspace_sync_enabled ? "translate-x-6" : "translate-x-1"
                        )} />
                    </button>
                </div>

                {/* Session length — how long before users must sign in again */}
                <div className="space-y-2">
                    <label className="text-sm font-medium text-gray-700 flex items-center gap-2">
                        <Clock className="h-4 w-4" />
                        Session Length (hours)
                    </label>
                    <input
                        type="number"
                        min={0.25}
                        max={720}
                        step={0.25}
                        value={workspaceConfig?.settings.session_lifetime_hours ?? ''}
                        onChange={(e) => setWorkspaceConfig(prev => prev ? {
                            ...prev,
                            settings: {
                                ...prev.settings,
                                session_lifetime_hours: e.target.value === '' ? null : Number(e.target.value)
                            }
                        } : null)}
                        placeholder="5 (default)"
                        className="w-full max-w-xs px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-brand/50 focus:border-transparent outline-none transition-all"
                    />
                    <p className="text-xs text-gray-500">
                        How long users stay signed in before Cubie requires a new Google sign-in.
                        Leave blank for the default (5 hours). Applies to sessions started after saving.
                    </p>
                </div>
            </div>

            {/* Mappings Section */}
            <div className="space-y-4">
                <h3 className="text-lg font-semibold text-gray-900">Group to Role Mappings</h3>
                <div className="bg-gray-50 rounded-xl p-4 space-y-4">
                    {workspaceConfig && Object.entries(workspaceConfig.mappings).map(([group, roles_in_group]) => (
                        <div key={group} className="flex flex-col md:flex-row md:items-center gap-4 bg-white p-3 rounded-lg border border-gray-100 shadow-sm">
                            <div className="flex-1">
                                <span className="text-sm font-medium text-gray-900">{group}</span>
                            </div>
                            <div className="flex flex-wrap gap-2">
                                {roles_in_group.map(role => (
                                    <span key={role} className="flex items-center gap-1 px-2 py-1 bg-brand-tint text-brand text-xs font-semibold rounded-md">
                                        {role}
                                        <button onClick={() => {
                                            const newMappings = { ...workspaceConfig.mappings };
                                            newMappings[group] = roles_in_group.filter(r => r !== role);
                                            setWorkspaceConfig({ ...workspaceConfig, mappings: newMappings });
                                        }}>×</button>
                                    </span>
                                ))}
                                {addingRoleToGroup === group ? (
                                    <div className="flex items-center gap-2 animate-in fade-in zoom-in-95 duration-200">
                                        <select
                                            className="text-xs border border-brand/20 rounded px-2 py-1 outline-none focus:ring-1 focus:ring-brand bg-white"
                                            autoFocus
                                            onChange={(e) => {
                                                const selectedRole = e.target.value;
                                                if (selectedRole && workspaceConfig) {
                                                    const newMappings = { ...workspaceConfig.mappings };
                                                    if (!newMappings[group].includes(selectedRole)) {
                                                        newMappings[group] = [...roles_in_group, selectedRole];
                                                        setWorkspaceConfig({ ...workspaceConfig, mappings: newMappings });
                                                    }
                                                }
                                                setAddingRoleToGroup(null);
                                            }}
                                            onBlur={() => setTimeout(() => setAddingRoleToGroup(null), 200)}
                                        >
                                            <option value="">Select Role...</option>
                                            {roles.map(r => (
                                                <option key={r.name} value={r.name} disabled={roles_in_group.includes(r.name)}>
                                                    {r.name}
                                                </option>
                                            ))}
                                        </select>
                                        <button
                                            onClick={() => setAddingRoleToGroup(null)}
                                            className="text-xs text-gray-400 hover:text-gray-600"
                                        >
                                            Cancel
                                        </button>
                                    </div>
                                ) : (
                                    <button
                                        className="text-xs text-brand hover:underline flex items-center gap-1"
                                        onClick={() => setAddingRoleToGroup(group)}
                                    >
                                        <Plus className="h-3 w-3" />
                                        Add Role
                                    </button>
                                )}
                            </div>
                            <button
                                onClick={() => {
                                    const newMappings = { ...workspaceConfig.mappings };
                                    delete newMappings[group];
                                    setWorkspaceConfig({ ...workspaceConfig, mappings: newMappings });
                                }}
                                className="text-red-400 hover:text-red-600 p-2"
                            >
                                <Trash2 className="h-4 w-4" />
                            </button>
                        </div>
                    ))}

                    {isAddingGroup ? (
                        <div className="flex flex-col gap-3 bg-white p-4 rounded-lg border border-brand/20 shadow-sm animate-in fade-in slide-in-from-top-2">
                            <div className="space-y-3">
                                <label className="block text-sm font-medium text-gray-700">Select Google Group</label>
                                {workspaceGroups.length > 0 ? (
                                    <select
                                        value={newGroupEmail}
                                        onChange={(e) => setNewGroupEmail(e.target.value)}
                                        className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-brand/50 outline-none transition-all"
                                        autoFocus
                                    >
                                        <option value="">Select a group...</option>
                                        {workspaceGroups.map(group => (
                                            <option key={group.email} value={group.email}>
                                                {group.name} ({group.email})
                                            </option>
                                        ))}
                                    </select>
                                ) : (
                                    <div className="text-sm text-amber-600 bg-amber-50 p-2 rounded border border-amber-100 italic">
                                        No groups found or Workspace sync is not fully configured.
                                    </div>
                                )}

                                <div className="relative">
                                    <div className="absolute inset-0 flex items-center" aria-hidden="true">
                                        <div className="w-full border-t border-gray-200"></div>
                                    </div>
                                    <div className="relative flex justify-center text-xs uppercase">
                                        <span className="bg-white px-2 text-gray-500 font-medium">Or enter manually</span>
                                    </div>
                                </div>

                                <input
                                    type="email"
                                    value={newGroupEmail}
                                    onChange={(e) => setNewGroupEmail(e.target.value)}
                                    placeholder="Enter Google Group Email (e.g. admins@example.com)"
                                    className="w-full px-4 py-2 rounded-lg border border-gray-200 focus:ring-2 focus:ring-brand/50 outline-none transition-all"
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter') handleAddGroup();
                                        if (e.key === 'Escape') setIsAddingGroup(false);
                                    }}
                                />
                            </div>
                            <div className="flex gap-2 justify-end mt-2">
                                <button
                                    onClick={() => setIsAddingGroup(false)}
                                    className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-200 transition-colors"
                                >
                                    Cancel
                                </button>
                                <button
                                    onClick={handleAddGroup}
                                    disabled={!newGroupEmail}
                                    className="px-4 py-2 bg-brand text-white rounded-lg text-sm font-medium hover:bg-brand-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                >
                                    Add Group
                                </button>
                            </div>
                        </div>
                    ) : (
                        <button
                            onClick={() => setIsAddingGroup(true)}
                            className="w-full py-2 border-2 border-dashed border-gray-200 rounded-lg text-gray-400 hover:border-brand/50 hover:text-brand transition-all font-medium flex items-center justify-center gap-2"
                        >
                            <Plus className="h-4 w-4" />
                            Add New Group Membership Mapping
                        </button>
                    )}
                </div>
            </div>

            <div className="pt-6 border-t border-gray-100 flex justify-end">
                <button
                    onClick={onSave}
                    disabled={saving}
                    className="bg-brand hover:bg-brand-hover text-white px-6 py-2 rounded-xl font-semibold flex items-center gap-2 transition-all disabled:opacity-50"
                >
                    {saving ? <div className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full" /> : <Save className="h-4 w-4" />}
                    Save Settings
                </button>
            </div>
        </motion.div>
    );
};

// Component: Access Control Tab
const AccessControlTab: React.FC<{
    groups: Group[];
    policies: AccessPolicy[];
    users: UserProfile[];
    departments: string[];
    topics: string[];
    onRefresh: () => void;
    setMessage: (msg: { type: 'success' | 'error', text: string } | null) => void;
}> = ({ groups, policies, users, departments, topics, onRefresh, setMessage }) => {
    return (
        <motion.div
            key="access"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            className="space-y-8"
        >
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                <GroupSection
                    groups={groups}
                    users={users}
                    onRefresh={onRefresh}
                    setMessage={setMessage}
                />
                <PolicySection
                    policies={policies}
                    groups={groups}
                    users={users}
                    departments={departments}
                    topics={topics}
                    onRefresh={onRefresh}
                    setMessage={setMessage}
                />
            </div>
        </motion.div>
    );
};

// Sub-component: Group Management
const GroupSection: React.FC<{
    groups: Group[];
    users: UserProfile[];
    onRefresh: () => void;
    setMessage: (msg: { type: 'success' | 'error', text: string } | null) => void;
}> = ({ groups, users, onRefresh, setMessage }) => {
    const [isAdding, setIsAdding] = useState(false);
    const [newGroupName, setNewGroupName] = useState('');
    const [newGroupDesc, setNewGroupDesc] = useState('');
    const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
    const [newUserEmail, setNewUserEmail] = useState('');

    const handleCreateGroup = async () => {
        if (!newGroupName.trim()) return;
        try {
            await client.post('/api/groups/', { name: newGroupName, description: newGroupDesc });
            setNewGroupName('');
            setNewGroupDesc('');
            setIsAdding(false);
            onRefresh();
            setMessage({ type: 'success', text: `Group "${newGroupName}" created.` });
        } catch (err: any) {
            setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to create group.' });
        }
    };

    const handleDeleteGroup = async (id: string, name: string) => {
        if (!confirm(`Delete group "${name}"?`)) return;
        try {
            await client.delete(`/api/groups/${id}`);
            onRefresh();
            setMessage({ type: 'success', text: `Group "${name}" deleted.` });
        } catch (err) {
            setMessage({ type: 'error', text: 'Failed to delete group.' });
        }
    };

    const handleAddMember = async (groupId: string) => {
        if (!newUserEmail.trim()) return;
        try {
            await client.post(`/api/groups/${groupId}/members`, { email: newUserEmail });
            setNewUserEmail('');
            onRefresh();
            setMessage({ type: 'success', text: `Added ${newUserEmail} to group.` });
        } catch (err: any) {
            setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to add member.' });
        }
    };

    const handleRemoveMember = async (groupId: string, email: string) => {
        try {
            await client.delete(`/api/groups/${groupId}/members/${email}`);
            onRefresh();
        } catch (err) {
            setMessage({ type: 'error', text: 'Failed to remove member.' });
        }
    };

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between">
                <h3 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                    <Users className="h-5 w-5 text-brand" />
                    Groups
                </h3>
                <button
                    onClick={() => setIsAdding(!isAdding)}
                    className="text-sm font-medium text-brand hover:underline flex items-center gap-1"
                >
                    <Plus className="h-4 w-4" />
                    New Group
                </button>
            </div>

            {isAdding && (
                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100 space-y-3 animate-in fade-in slide-in-from-top-2">
                    <input
                        type="text"
                        placeholder="Group Name (e.g. Sales-Team)"
                        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:ring-1 focus:ring-brand"
                        value={newGroupName}
                        onChange={e => setNewGroupName(e.target.value)}
                    />
                    <input
                        type="text"
                        placeholder="Description (optional)"
                        className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none focus:ring-1 focus:ring-brand"
                        value={newGroupDesc}
                        onChange={e => setNewGroupDesc(e.target.value)}
                    />
                    <div className="flex gap-2">
                        <button
                            onClick={handleCreateGroup}
                            className="bg-brand text-white px-4 py-1.5 rounded-lg text-xs font-semibold"
                        >
                            Create
                        </button>
                        <button
                            onClick={() => setIsAdding(false)}
                            className="bg-gray-200 text-gray-700 px-4 py-1.5 rounded-lg text-xs font-semibold"
                        >
                            Cancel
                        </button>
                    </div>
                </div>
            )}

            <div className="space-y-3">
                {groups.map(group => (
                    <div key={group.id} className="border border-gray-100 rounded-xl overflow-hidden bg-white shadow-sm">
                        <div className="p-4 flex items-center justify-between">
                            <div className="flex items-center gap-3 cursor-pointer flex-1" onClick={() => setExpandedGroup(expandedGroup === group.id ? null : group.id)}>
                                <div className="bg-brand-tint p-2 rounded-lg text-brand">
                                    <Users className="h-4 w-4" />
                                </div>
                                <div>
                                    <span className="font-semibold text-gray-900 block">{group.name}</span>
                                    {group.description && <span className="text-xs text-gray-500">{group.description}</span>}
                                </div>
                                <ChevronRight className={cn("h-4 w-4 text-gray-400 transition-transform", expandedGroup === group.id && "rotate-90")} />
                            </div>
                            <button onClick={() => handleDeleteGroup(group.id, group.name)} className="text-gray-400 hover:text-red-500 p-1">
                                <Trash2 className="h-4 w-4" />
                            </button>
                        </div>

                        <AnimatePresence>
                            {expandedGroup === group.id && (
                                <motion.div
                                    initial={{ height: 0, opacity: 0 }}
                                    animate={{ height: 'auto', opacity: 1 }}
                                    exit={{ height: 0, opacity: 0 }}
                                    className="px-4 pb-4 border-t border-gray-50"
                                >
                                    <div className="pt-4 space-y-4">
                                        <div className="flex gap-2">
                                            <select
                                                className="flex-1 px-3 py-1.5 text-xs border border-gray-200 rounded-lg outline-none focus:ring-1 focus:ring-brand"
                                                value={newUserEmail}
                                                onChange={e => setNewUserEmail(e.target.value)}
                                            >
                                                <option value="">Select user to add...</option>
                                                {users.map(u => <option key={u.email} value={u.email}>{u.name} ({u.email})</option>)}
                                            </select>
                                            <button
                                                onClick={() => handleAddMember(group.id)}
                                                className="bg-[#1e3a5f] text-white px-3 py-1.5 rounded-lg text-xs font-semibold"
                                            >
                                                Add
                                            </button>
                                        </div>
                                        <div className="space-y-1">
                                            <p className="text-[10px] uppercase font-bold text-gray-400 mb-1">Current Members (Syncing...)</p>
                                            <div className="flex flex-wrap gap-2">
                                                {users.filter(u => u.workspace_groups?.includes(group.name)).map(u => (
                                                    <div key={u.email} className="flex items-center gap-1 bg-gray-100 px-2 py-1 rounded text-[10px] text-gray-600 border border-gray-200">
                                                        <span>{u.email}</span>
                                                        <button onClick={() => handleRemoveMember(group.id, u.email)} className="hover:text-red-500">×</button>
                                                    </div>
                                                ))}
                                                {users.filter(u => u.workspace_groups?.includes(group.name)).length === 0 && (
                                                    <p className="text-[10px] text-gray-400 italic">No members found in local cache.</p>
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                </motion.div>
                            )}
                        </AnimatePresence>
                    </div>
                ))}
            </div>
        </div>
    );
};

// Sub-component: Policy Management
const PolicySection: React.FC<{
    policies: AccessPolicy[];
    groups: Group[];
    users: UserProfile[];
    departments: string[];
    topics: string[];
    onRefresh: () => void;
    setMessage: (msg: { type: 'success' | 'error', text: string } | null) => void;
}> = ({ policies, groups, users, departments, topics, onRefresh, setMessage }) => {
    const [isAdding, setIsAdding] = useState(false);
    const [targetType, setTargetType] = useState<'user' | 'group'>('user');
    const [targetId, setTargetId] = useState('');
    const [selectedDept, setSelectedDept] = useState('');
    const [selectedTopic, setSelectedTopic] = useState('');

    const handleCreatePolicy = async () => {
        if (!targetId) return;
        const payload: any = {
            department: selectedDept || null,
            topic: selectedTopic || null,
            can_read: true
        };
        if (targetType === 'user') payload.user_email = targetId;
        else payload.group_id = targetId;

        try {
            await client.post('/api/policies/', payload);
            setIsAdding(false);
            onRefresh();
            setMessage({ type: 'success', text: 'Access policy created.' });
        } catch (err: any) {
            setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to create policy.' });
        }
    };

    const handleDeletePolicy = async (id: number) => {
        try {
            await client.delete(`/api/policies/${id}`);
            onRefresh();
            setMessage({ type: 'success', text: 'Policy removed.' });
        } catch (err) {
            setMessage({ type: 'error', text: 'Failed to delete policy.' });
        }
    };

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between">
                <h3 className="text-lg font-bold text-gray-900 flex items-center gap-2">
                    <Lock className="h-5 w-5 text-brand" />
                    Access Policies
                </h3>
                <button
                    onClick={() => setIsAdding(!isAdding)}
                    className="text-sm font-medium text-brand hover:underline flex items-center gap-1"
                >
                    <Plus className="h-4 w-4" />
                    New Policy
                </button>
            </div>

            {isAdding && (
                <div className="p-4 bg-gray-50 rounded-xl border border-gray-100 space-y-4 animate-in fade-in slide-in-from-top-2">
                    <div className="flex gap-2 mb-2">
                        <button
                            onClick={() => { setTargetType('user'); setTargetId(''); }}
                            className={cn("flex-1 py-1 text-xs rounded-md border", targetType === 'user' ? "bg-brand text-white border-brand" : "bg-white text-gray-600 border-gray-200")}
                        >
                            Individual User
                        </button>
                        <button
                            onClick={() => { setTargetType('group'); setTargetId(''); }}
                            className={cn("flex-1 py-1 text-xs rounded-md border", targetType === 'group' ? "bg-brand text-white border-brand" : "bg-white text-gray-600 border-gray-200")}
                        >
                            Group
                        </button>
                    </div>

                    <div className="grid grid-cols-1 gap-3">
                        {targetType === 'user' ? (
                            <select
                                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none"
                                value={targetId}
                                onChange={e => setTargetId(e.target.value)}
                            >
                                <option value="">Select User...</option>
                                {users.map(u => <option key={u.email} value={u.email}>{u.name} ({u.email})</option>)}
                            </select>
                        ) : (
                            <select
                                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none"
                                value={targetId}
                                onChange={e => setTargetId(e.target.value)}
                            >
                                <option value="">Select Group...</option>
                                {groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
                            </select>
                        )}

                        <div className="grid grid-cols-2 gap-3">
                            <div>
                                <label className="text-[10px] font-bold text-gray-400 uppercase mb-1 block">Department</label>
                                <select
                                    className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none"
                                    value={selectedDept}
                                    onChange={e => setSelectedDept(e.target.value)}
                                >
                                    <option value="">All Departments</option>
                                    {departments.map(d => <option key={d} value={d}>{d}</option>)}
                                </select>
                            </div>
                            <div>
                                <label className="text-[10px] font-bold text-gray-400 uppercase mb-1 block">Topic</label>
                                <select
                                    className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg outline-none"
                                    value={selectedTopic}
                                    onChange={e => setSelectedTopic(e.target.value)}
                                >
                                    <option value="">All Topics</option>
                                    {topics.map(t => <option key={t} value={t}>{t}</option>)}
                                </select>
                            </div>
                        </div>
                    </div>

                    <div className="flex gap-2 pt-2">
                        <button onClick={handleCreatePolicy} className="flex-1 bg-brand text-white py-2 rounded-lg text-sm font-semibold">
                            Grant Access
                        </button>
                        <button onClick={() => setIsAdding(false)} className="flex-1 bg-gray-200 text-gray-700 py-2 rounded-lg text-sm font-semibold">
                            Cancel
                        </button>
                    </div>
                </div>
            )}

            <div className="bg-white border border-gray-100 rounded-xl overflow-hidden shadow-sm">
                <table className="w-full text-left text-sm">
                    <thead>
                        <tr className="bg-gray-50/50 border-b border-gray-100 text-gray-500">
                            <th className="py-3 px-4 font-semibold">Subject</th>
                            <th className="py-3 px-4 font-semibold">Resource Permissions</th>
                            <th className="py-3 px-4 font-semibold w-12"></th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                        {policies.map(policy => (
                            <tr key={policy.id} className="hover:bg-gray-50/30 transition-colors">
                                <td className="py-3 px-4">
                                    <div className="flex items-center gap-2">
                                        <Key className="h-3.5 w-3.5 text-gray-400" />
                                        {policy.user_email ? (
                                            <>
                                                <User className="h-3 w-3 text-blue-500" />
                                                <span className="font-medium">{policy.user_email}</span>
                                            </>
                                        ) : (
                                            <>
                                                <Users className="h-3 w-3 text-brand" />
                                                <span className="font-medium">{policy.group_name || group_id_to_name(policy.group_id, groups)}</span>
                                            </>
                                        )}
                                    </div>
                                </td>
                                <td className="py-3 px-4">
                                    <div className="flex flex-wrap gap-1">
                                        <span className="px-2 py-0.5 bg-green-50 text-green-700 text-[10px] font-bold rounded border border-green-100">READ</span>
                                        {policy.department && (
                                            <span className="px-2 py-0.5 bg-gray-100 text-gray-600 text-[10px] font-bold rounded border border-gray-200">DEPT: {policy.department.toUpperCase()}</span>
                                        )}
                                        {policy.topic && (
                                            <span className="px-2 py-0.5 bg-gray-100 text-gray-600 text-[10px] font-bold rounded border border-gray-200">TOPIC: {policy.topic.toUpperCase()}</span>
                                        )}
                                        {!policy.department && !policy.topic && (
                                            <span className="px-2 py-0.5 bg-blue-50 text-blue-700 text-[10px] font-bold rounded border border-blue-100 uppercase">Global</span>
                                        )}
                                    </div>
                                </td>
                                <td className="py-3 px-4">
                                    <button onClick={() => handleDeletePolicy(policy.id)} className="text-gray-300 hover:text-red-500 transition-colors">
                                        <Trash2 className="h-4 w-4" />
                                    </button>
                                </td>
                            </tr>
                        ))}
                        {policies.length === 0 && (
                            <tr>
                                <td colSpan={3} className="py-8 text-center text-gray-400 italic">No custom policies defined.</td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
};

// Helper
const group_id_to_name = (id: string | undefined, groups: Group[]) => {
    if (!id) return 'Unknown Group';
    return groups.find(g => g.id === id)?.name || id;
};
