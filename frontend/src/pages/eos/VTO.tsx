import React, { useCallback, useEffect, useState } from 'react';
import { Target, Upload, Download, Loader2, CheckCircle2, AlertCircle, Lock } from 'lucide-react';
import client from '../../api/client';
import { templateFilename } from '../../utils/download';
import { useAuth } from '../../context/AuthContext';
import {
    EmptyState,
    ErrorState,
    EosPageHeader,
    LoadingState,
    PageContainer,
} from './_shared';

interface VTOVersion {
    id: string;
    version_name: string;
    is_active: boolean;
    status: string;
}

interface Bullet {
    text: string;
    confidential: boolean;
}

interface VTOSection {
    id: string;
    section_type: string;
    // The backend always sends the new `{text, confidential}[]` shape
    // (bullets_to_content); the looser types are defensive fallbacks only.
    content: Bullet[] | string[] | string | Record<string, unknown> | null;
}

const VISION_TYPES: Array<{ key: string; label: string }> = [
    { key: 'values', label: 'Core Values' },
    { key: 'focus', label: 'Core Focus' },
    { key: '10_year_target', label: '10-Year Target' },
];

const TRACTION_TYPES: Array<{ key: string; label: string }> = [
    { key: '3_year_picture', label: '3-Year Picture' },
    { key: '1_year_plan', label: '1-Year Plan' },
    { key: 'quarterly_rocks', label: 'Quarterly Rocks' },
];

/** Normalize a section's content into typed bullets. The backend always
 * sends `{text, confidential}[]` (confidential bullets are already
 * stripped out server-side for non-exec viewers — this never has to
 * decide who's allowed to see what, only how to render what arrived);
 * the legacy-shape branches are defensive fallbacks only. */
const toBullets = (content: VTOSection['content']): Bullet[] => {
    if (Array.isArray(content)) {
        return content
            .map((c): Bullet => {
                if (c && typeof c === 'object' && 'text' in c) {
                    return { text: String((c as Bullet).text ?? ''), confidential: Boolean((c as Bullet).confidential) };
                }
                return { text: String(c), confidential: false };
            })
            .filter(b => b.text);
    }
    if (typeof content === 'string' && content.trim()) return [{ text: content, confidential: false }];
    if (content && typeof content === 'object') return [{ text: JSON.stringify(content), confidential: false }];
    return [];
};

/** Read-only section card. The VTO is maintained by the Exec team via the
 * Excel upload above; this is how everyone at the company sees that source of
 * truth. A "Confidential" badge can only ever appear here for an Exec/admin
 * viewer — the backend never sends a confidential bullet to anyone else. */
const SectionBox: React.FC<{
    label: string;
    section: VTOSection | undefined;
}> = ({ label, section }) => {
    const bullets = toBullets(section?.content ?? null);
    return (
        <div>
            <h3 className="text-xs font-bold uppercase tracking-wider text-brand mb-2">
                {label}
            </h3>
            <div className="bg-gray-50 border border-gray-200 rounded-md p-4 min-h-[80px]">
                {bullets.length === 0 ? (
                    <p className="text-sm text-gray-400 italic">Not yet set.</p>
                ) : (
                    <ul className="space-y-1.5">
                        {bullets.map((item, i) => (
                            <li key={i} className="flex items-start gap-2 text-sm text-gray-700">
                                <span className="text-brand font-bold mt-0.5">•</span>
                                <span className="leading-snug">{item.text}</span>
                                {item.confidential && (
                                    <span
                                        title="Confidential — only the Exec team and admins can see this line"
                                        className="inline-flex items-center gap-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 bg-amber-50 border border-amber-200 rounded px-1 py-0.5 mt-0.5 flex-shrink-0"
                                    >
                                        <Lock size={10} /> Confidential
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
};

/** Exec-only uploader. The VTO page itself is company-wide now, so unlike
 * before, this component is only rendered by the parent for Exec/admin
 * viewers (see the `VTO` component below) — the backend re-checks on
 * every call regardless. */
const VTOUploader: React.FC<{ onPublished: () => Promise<void> | void }> = ({ onPublished }) => {
    const [file, setFile] = useState<File | null>(null);
    const [uploading, setUploading] = useState(false);
    const [downloading, setDownloading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [message, setMessage] = useState<string | null>(null);

    const downloadTemplate = async () => {
        setDownloading(true);
        setError(null);
        try {
            const res = await client.get('/api/eos/vto/template', { responseType: 'blob' });
            // Honour the filename the server chose, as the Scorecard does.
            const url = URL.createObjectURL(res.data as Blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = templateFilename(res.headers?.['content-disposition'], 'VTO');
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch {
            setError('Could not download the template. Please try again.');
        } finally {
            setDownloading(false);
        }
    };

    const upload = async () => {
        if (!file) return;
        setUploading(true);
        setError(null);
        setMessage(null);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const { data } = await client.post('/api/eos/vto/upload', formData, {
                headers: { 'Content-Type': 'multipart/form-data' },
            });
            const count = (data.sections_set || []).length;
            const ignored: string[] = data.ignored_labels || [];
            let msg = `Published "${data.version_name}" with ${count} section${count === 1 ? '' : 's'}.`;
            if (ignored.length) msg += ` Ignored unrecognized section(s): ${ignored.join(', ')}.`;
            setMessage(msg);
            setFile(null);
            const input = document.getElementById('vto-file-input') as HTMLInputElement | null;
            if (input) input.value = '';
            await onPublished();
        } catch (err: any) {
            setError(
                err?.response?.data?.detail ||
                    'Upload failed. Make sure you uploaded the filled-in .xlsx template.',
            );
        } finally {
            setUploading(false);
        }
    };

    return (
        <div className="mb-5 bg-white border border-gray-200 rounded-lg shadow-sm p-4">
            <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                    <h3 className="text-sm font-bold text-gray-800">Publish the VTO</h3>
                    <p className="text-xs text-gray-500 mt-0.5 max-w-xl">
                        Download the Excel template and add one row per goal — choose its Section
                        from the dropdown, and mark Confidential "Yes" for anything sensitive (leave
                        it blank or "No" for lines everyone at your company should see). Each upload publishes
                        a new active version and archives the previous one.
                    </p>
                </div>
                <button
                    onClick={downloadTemplate}
                    disabled={downloading}
                    className="inline-flex items-center gap-1.5 text-sm font-medium text-brand hover:text-brand-hover disabled:opacity-50"
                >
                    {downloading ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
                    Download template
                </button>
            </div>

            <div className="mt-3 flex items-center gap-3 flex-wrap">
                <input
                    id="vto-file-input"
                    type="file"
                    accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    onChange={e => {
                        setFile(e.target.files?.[0] ?? null);
                        setError(null);
                        setMessage(null);
                    }}
                    className="block text-sm text-gray-600 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border-0 file:text-sm file:font-medium file:bg-gray-100 file:text-gray-700 hover:file:bg-gray-200"
                />
                <button
                    onClick={upload}
                    disabled={!file || uploading}
                    className="inline-flex items-center gap-1.5 bg-brand text-white text-sm font-medium px-4 py-1.5 rounded-md hover:bg-brand-hover disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    {uploading ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />}
                    {uploading ? 'Publishing…' : 'Upload & publish'}
                </button>
            </div>

            {message && (
                <div className="mt-3 flex items-start gap-2 text-xs text-green-800 bg-green-50 border border-green-200 rounded-md px-3 py-2">
                    <CheckCircle2 size={14} className="flex-shrink-0 mt-0.5" />
                    <span>{message}</span>
                </div>
            )}
            {error && (
                <div className="mt-3 flex items-start gap-2 text-xs text-red-800 bg-red-50 border border-red-200 rounded-md px-3 py-2">
                    <AlertCircle size={14} className="flex-shrink-0 mt-0.5" />
                    <span>{error}</span>
                </div>
            )}
        </div>
    );
};

export const VTO: React.FC = () => {
    const { isExec, isAdmin } = useAuth();
    const canPublish = isExec || isAdmin;
    const [version, setVersion] = useState<VTOVersion | null>(null);
    const [sections, setSections] = useState<VTOSection[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const load = useCallback(async () => {
        setError(null);
        try {
            const vRes = await client.get<VTOVersion[]>('/api/eos/vto/versions');
            const active = vRes.data.find(v => v.is_active) || vRes.data[0] || null;
            setVersion(active);
            if (!active) {
                setSections([]);
                return;
            }
            const sRes = await client.get<VTOSection[]>(
                `/api/eos/vto/versions/${active.id}/sections`,
            );
            setSections(sRes.data);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Unknown error');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    const sectionFor = (type: string) => sections.find(s => s.section_type === type);

    return (
        <PageContainer>
            <EosPageHeader
                icon={<Target size={20} />}
                title="VTO"
                subtitle={version ? version.version_name : 'Vision / Traction Organizer'}
            />

            {!loading && canPublish && <VTOUploader onPublished={load} />}

            {loading && <LoadingState label="Loading VTO…" />}
            {error && <ErrorState message={error} />}

            {!loading && !error && !version && (
                <EmptyState
                    title="No VTO published yet."
                    hint="Download the template above, fill it in, and upload it to publish your first VTO."
                />
            )}

            {!loading && !error && version && (
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden" data-tour="eos-vto-vision">
                        <div className="bg-brand text-white px-6 py-3 font-bold text-base">
                            VISION
                        </div>
                        <div className="p-6 space-y-6">
                            {VISION_TYPES.map(t => (
                                <SectionBox
                                    key={t.key}
                                    label={t.label}
                                    section={sectionFor(t.key)}
                                />
                            ))}
                        </div>
                    </section>

                    <section className="bg-white rounded-lg border border-gray-200 shadow-sm overflow-hidden" data-tour="eos-vto-traction">
                        <div className="bg-brand-hover text-white px-6 py-3 font-bold text-base">
                            TRACTION
                        </div>
                        <div className="p-6 space-y-6">
                            {TRACTION_TYPES.map(t => (
                                <SectionBox
                                    key={t.key}
                                    label={t.label}
                                    section={sectionFor(t.key)}
                                />
                            ))}
                        </div>
                    </section>
                </div>
            )}
        </PageContainer>
    );
};
