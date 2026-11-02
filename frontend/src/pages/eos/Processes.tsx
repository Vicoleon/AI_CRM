import React, { useEffect, useMemo, useState } from 'react';
import { Workflow, Plus, ExternalLink } from 'lucide-react';
import client from '../../api/client';
import {
    EmptyState,
    ErrorState,
    EosPageHeader,
    LoadingState,
    PageContainer,
} from './_shared';
import { formatPerson } from './_utils';
import type { PersonLite } from './_utils';
import { Modal, Field, inputCls, SubmitButton } from '../../components/eos/Modal';

interface Process {
    id: string;
    name: string;
    owner_id: string | null;
    documentation_url: string | null;
}

interface FormState {
    name: string;
    owner_id: string;
    documentation_url: string;
}

const EMPTY_FORM: FormState = { name: '', owner_id: '', documentation_url: '' };

export const Processes: React.FC = () => {
    const [processes, setProcesses] = useState<Process[]>([]);
    const [people, setPeople] = useState<PersonLite[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const [modalOpen, setModalOpen] = useState(false);
    const [form, setForm] = useState<FormState>(EMPTY_FORM);
    const [submitting, setSubmitting] = useState(false);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [prRes, peRes] = await Promise.all([
                    client.get<Process[]>('/api/eos/processes/'),
                    client.get<PersonLite[]>('/api/eos/people/'),
                ]);
                if (cancelled) return;
                setProcesses(prRes.data);
                setPeople(peRes.data);
            } catch (err) {
                if (!cancelled) setError(err instanceof Error ? err.message : 'Unknown error');
            } finally {
                if (!cancelled) setLoading(false);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    const peopleById = useMemo(() => new Map(people.map(p => [p.id, p])), [people]);

    const openNew = () => {
        setForm({ ...EMPTY_FORM, owner_id: people[0]?.id ?? '' });
        setModalOpen(true);
    };

    const onSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setSubmitting(true);
        try {
            const payload: Record<string, unknown> = { name: form.name };
            if (form.owner_id) payload.owner_id = form.owner_id;
            if (form.documentation_url) payload.documentation_url = form.documentation_url;
            const res = await client.post<Process>('/api/eos/processes/', payload);
            setProcesses(prev => [...prev, res.data]);
            setModalOpen(false);
        } catch (err) {
            console.error('Failed to create process', err);
            alert('Failed to create process.');
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <PageContainer>
            <EosPageHeader
                icon={<Workflow size={20} />}
                title="Core Processes"
                subtitle="FBA (Followed By All) documents — the documented way your company runs"
                right={
                    <button
                        type="button"
                        onClick={openNew}
                        className="flex items-center gap-2 px-3 py-2 bg-brand hover:bg-brand-hover text-white rounded-md text-sm font-medium transition-colors"
                        data-tour="eos-processes-new"
                    >
                        <Plus size={16} /> New Process
                    </button>
                }
            />

            {loading && <LoadingState label="Loading processes…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && processes.length === 0 && (
                <EmptyState
                    title="No core processes documented yet."
                    hint='Click "New Process" above to add the first one.'
                />
            )}

            {!loading && !error && processes.length > 0 && (
                <div className="bg-white rounded-lg border border-gray-200 shadow-sm divide-y divide-gray-100" data-tour="eos-processes-list">
                    {processes.map(p => (
                        <div
                            key={p.id}
                            className="flex items-center gap-4 px-5 py-4"
                        >
                            <div className="bg-gray-100 text-gray-500 p-2 rounded-md">
                                <Workflow size={18} />
                            </div>
                            <div className="flex-1 min-w-0">
                                <p className="text-base font-semibold text-gray-900 truncate">
                                    {p.name}
                                </p>
                                <p className="text-xs text-gray-500 mt-0.5">
                                    Owner:{' '}
                                    <span className="text-brand">
                                        {formatPerson(peopleById.get(p.owner_id ?? ''))}
                                    </span>
                                </p>
                            </div>
                            {p.documentation_url && (
                                <a
                                    href={p.documentation_url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-brand hover:bg-brand/5 rounded-md font-medium"
                                >
                                    <ExternalLink size={14} /> Open
                                </a>
                            )}
                        </div>
                    ))}
                </div>
            )}

            <Modal
                isOpen={modalOpen}
                onClose={() => setModalOpen(false)}
                title="New Core Process"
            >
                <form onSubmit={onSubmit} className="space-y-4">
                    <Field label="Process name">
                        <input
                            required
                            type="text"
                            value={form.name}
                            onChange={e => setForm({ ...form, name: e.target.value })}
                            className={inputCls}
                            placeholder='e.g. "New client onboarding"'
                            autoFocus
                        />
                    </Field>
                    <Field label="Owner (optional)">
                        <select
                            value={form.owner_id}
                            onChange={e => setForm({ ...form, owner_id: e.target.value })}
                            className={inputCls}
                        >
                            <option value="">— No owner —</option>
                            {people.map(p => (
                                <option key={p.id} value={p.id}>
                                    {p.first_name} {p.last_name}
                                </option>
                            ))}
                        </select>
                    </Field>
                    <Field label="Documentation URL (optional)">
                        <input
                            type="url"
                            value={form.documentation_url}
                            onChange={e => setForm({ ...form, documentation_url: e.target.value })}
                            className={inputCls}
                            placeholder="https://…"
                        />
                    </Field>
                    <SubmitButton disabled={submitting} onCancel={() => setModalOpen(false)}>
                        Create process
                    </SubmitButton>
                </form>
            </Modal>
        </PageContainer>
    );
};
