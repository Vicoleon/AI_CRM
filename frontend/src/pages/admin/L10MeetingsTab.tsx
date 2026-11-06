import React, { useState, useEffect } from 'react';
import { Calendar, RefreshCw, UserCog, AlertTriangle } from 'lucide-react';
import client from '../../api/client';
import { parseInstant } from '../../utils/datetime';
import { toast } from '../../components/eos/Toast';
import { confirmDialog } from '../../components/eos/ConfirmDialog';

/**
 * Admin-only L10 switchboard.
 *
 * Admins have always been allowed to re-point any L10 meeting, but never to
 * *enumerate* them: L10 read access is membership-scoped for everyone, admins
 * included, so an admin asked to rescue a team whose facilitator went on
 * vacation had the authority and no way to find the row. This tab is the missing
 * half — the list, plus a Reassign Manager action.
 *
 * It deliberately shows STRUCTURE ONLY: name, team, schedule, who runs it, how
 * many people are on it, when it last met. No notes, diary, headlines, ratings,
 * or attendee identities — the membership-only content fence stays exactly where
 * it is. A number, not a roster, is what keeps listing the Exec L10 from
 * disclosing who's on the Exec team.
 */
interface AdminMeeting {
    id: string;
    name: string;
    department_name: string | null;
    is_exec_meeting: boolean;
    day_of_week: number | null;
    start_time: string | null;
    duration_minutes: number;
    manager_id: string | null;
    manager_name: string | null;
    manager_email: string | null;
    attendee_count: number;
    last_session_at: string | null;
}

interface PersonLite {
    id: string;
    first_name: string;
    last_name: string;
    email: string;
}

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

const schedule = (m: AdminMeeting): string => {
    if (m.day_of_week === null) return 'Unscheduled';
    const day = DAYS[m.day_of_week] ?? '?';
    return m.start_time ? `${day} ${m.start_time}` : day;
};

const lastMet = (iso: string | null): string => {
    const d = parseInstant(iso);
    if (!d) return 'Never';
    const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
    if (days <= 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days < 30) return `${days}d ago`;
    return d.toLocaleDateString();
};

export const L10MeetingsTab: React.FC = () => {
    const [meetings, setMeetings] = useState<AdminMeeting[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [loading, setLoading] = useState(true);
    const [savingId, setSavingId] = useState<string | null>(null);

    useEffect(() => {
        void load();
    }, []);

    const load = async () => {
        setLoading(true);
        try {
            const [mRes, pRes] = await Promise.all([
                client.get<{ meetings: AdminMeeting[] }>('/api/eos/l10/admin/meetings'),
                client.get<PersonLite[]>('/api/eos/people/'),
            ]);
            setMeetings(mRes.data.meetings ?? []);
            setPeople(pRes.data ?? []);
        } catch (err) {
            console.error('Failed to load L10 meetings', err);
            toast.error('Could not load meetings.');
        } finally {
            setLoading(false);
        }
    };

    const reassign = async (meeting: AdminMeeting, personId: string) => {
        const person = people.find(p => p.id === personId);
        const label = person
            ? `${person.first_name} ${person.last_name}`.trim() || person.email
            : 'nobody';

        if (
            !(await confirmDialog({
                title: `Make ${label} the manager?`,
                message: `"${meeting.name}" will be run by ${label}${
                    meeting.manager_name ? `, replacing ${meeting.manager_name}` : ''
                }. They'll be able to start the meeting and drive the agenda. The change is logged against your account.`,
                confirmLabel: 'Reassign',
            }))
        ) {
            return;
        }

        setSavingId(meeting.id);
        try {
            await client.post(`/api/eos/l10/${meeting.id}/transfer-manager`, {
                new_manager_id: personId || null,
                note: 'admin console reassignment',
            });
            setMeetings(prev =>
                prev.map(m =>
                    m.id === meeting.id
                        ? {
                              ...m,
                              manager_id: personId || null,
                              manager_name: person
                                  ? `${person.first_name} ${person.last_name}`.trim()
                                  : null,
                              manager_email: person?.email ?? null,
                          }
                        : m,
                ),
            );
            toast.success(`${meeting.name} is now managed by ${label}.`);
        } catch (err) {
            console.error('Failed to reassign manager', err);
            const detail = (err as { response?: { data?: { detail?: string } } }).response
                ?.data?.detail;
            toast.error(detail ?? 'Could not reassign the manager.');
        } finally {
            setSavingId(null);
        }
    };

    const orphaned = meetings.filter(m => !m.manager_id).length;

    return (
        <div className="space-y-4">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h3 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
                        <Calendar size={18} className="text-brand" />
                        L10 Meetings
                    </h3>
                    <p className="text-sm text-gray-500 mt-1 max-w-2xl">
                        Every L10 in the company, so a team whose facilitator is away
                        isn't locked out of its own meeting. Structure only — meeting
                        notes, diaries and headlines stay private to each team.
                    </p>
                </div>
                <button
                    type="button"
                    onClick={load}
                    disabled={loading}
                    className="shrink-0 inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                >
                    <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
                    Refresh
                </button>
            </div>

            {orphaned > 0 && (
                <div className="flex items-start gap-2 text-sm bg-amber-50 border border-amber-200 text-amber-800 rounded-md px-3 py-2">
                    <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                    <span>
                        {orphaned} meeting{orphaned === 1 ? '' : 's'} ha
                        {orphaned === 1 ? 's' : 've'} no manager — nobody can start
                        {orphaned === 1 ? ' it' : ' them'} until someone is assigned.
                    </span>
                </div>
            )}

            {loading && meetings.length === 0 ? (
                <p className="text-sm text-gray-500">Loading meetings…</p>
            ) : meetings.length === 0 ? (
                <p className="text-sm text-gray-500">No L10 meetings exist yet.</p>
            ) : (
                <div className="overflow-x-auto border border-gray-200 rounded-lg">
                    <table className="min-w-full text-sm">
                        <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
                            <tr>
                                <th className="px-3 py-2 font-medium">Meeting</th>
                                <th className="px-3 py-2 font-medium">Team</th>
                                <th className="px-3 py-2 font-medium">When</th>
                                <th className="px-3 py-2 font-medium">People</th>
                                <th className="px-3 py-2 font-medium">Last met</th>
                                <th className="px-3 py-2 font-medium">Manager</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {meetings.map(m => (
                                <tr key={m.id} className="hover:bg-gray-50/60">
                                    <td className="px-3 py-2 font-medium text-gray-900">
                                        {m.name}
                                        {m.is_exec_meeting && (
                                            <span className="ml-2 text-[10px] font-bold uppercase tracking-wider bg-brand text-white px-1.5 py-0.5 rounded-full">
                                                Exec
                                            </span>
                                        )}
                                    </td>
                                    <td className="px-3 py-2 text-gray-600">
                                        {m.is_exec_meeting
                                            ? 'Exec team'
                                            : m.department_name || '—'}
                                    </td>
                                    <td className="px-3 py-2 text-gray-600 whitespace-nowrap">
                                        {schedule(m)}
                                    </td>
                                    <td className="px-3 py-2 text-gray-600">
                                        {m.attendee_count || '—'}
                                    </td>
                                    <td className="px-3 py-2 text-gray-600 whitespace-nowrap">
                                        {lastMet(m.last_session_at)}
                                    </td>
                                    <td className="px-3 py-2">
                                        <div className="flex items-center gap-2">
                                            <UserCog
                                                size={14}
                                                className={
                                                    m.manager_id
                                                        ? 'text-gray-400'
                                                        : 'text-amber-500'
                                                }
                                            />
                                            <select
                                                value={m.manager_id ?? ''}
                                                disabled={savingId === m.id}
                                                onChange={e =>
                                                    void reassign(m, e.target.value)
                                                }
                                                className="text-sm border border-gray-300 rounded px-2 py-1 bg-white disabled:opacity-50 max-w-[14rem]"
                                            >
                                                <option value="">
                                                    — No manager —
                                                </option>
                                                {people.map(p => (
                                                    <option key={p.id} value={p.id}>
                                                        {`${p.first_name} ${p.last_name}`.trim() ||
                                                            p.email}
                                                    </option>
                                                ))}
                                            </select>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
};

export default L10MeetingsTab;
