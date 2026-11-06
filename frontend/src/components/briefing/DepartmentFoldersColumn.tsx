// Team Folders tab on the Productivity page.
//
// Shows the shared Drive folders an admin curated for the user's department
// (plus any company-wide folders). People spend ~90% of their day in these, so
// this lands them in the right place in one click. Curation lives in
// Admin → Folders; this is the read-only consumer of /api/department-folders/mine.
import React, { useEffect, useState } from 'react';
import { FolderTree, ExternalLink, RefreshCw, AlertCircle } from 'lucide-react';
import client from '../../api/client';

interface DeptFolder {
    id: string;
    department: string | null;
    folder_id: string;
    folder_name: string;
    web_view_link: string | null;
    sort_order: number;
    is_active: boolean;
}

interface MineResponse {
    department: string | null;
    folders: DeptFolder[];
}

export const DepartmentFoldersColumn: React.FC = () => {
    const [data, setData] = useState<MineResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const fetchFolders = async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await client.get<MineResponse>('/api/department-folders/mine');
            setData(res.data);
        } catch (e: any) {
            setError(e?.response?.data?.detail || e.message || 'Failed to load folders');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => { fetchFolders(); }, []);

    const folders = data?.folders ?? [];
    const driveUrl = (f: DeptFolder) =>
        f.web_view_link || `https://drive.google.com/drive/folders/${f.folder_id}`;

    return (
        <div className="p-5">
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    <FolderTree className="h-4 w-4 text-brand" />
                    <h3 className="text-sm font-semibold text-gray-900">
                        Team Folders
                        {data?.department && data.department !== 'general' && (
                            <span className="text-gray-400 font-normal"> · {data.department}</span>
                        )}
                    </h3>
                </div>
                <button
                    onClick={fetchFolders}
                    disabled={loading}
                    className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-brand disabled:opacity-50"
                >
                    <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
                    Refresh
                </button>
            </div>

            <p className="text-xs text-gray-500 mb-4">
                The shared Drive folders your team works in. Curated by an admin in
                Admin&nbsp;→&nbsp;Folders.
            </p>

            {error && (
                <div className="p-2.5 mb-3 bg-red-50 text-red-700 rounded-md flex items-center gap-2 text-xs">
                    <AlertCircle className="h-4 w-4" /> {error}
                </div>
            )}

            {loading && !data ? (
                <div className="flex items-center justify-center py-10">
                    <RefreshCw className="h-5 w-5 animate-spin text-gray-400" />
                </div>
            ) : folders.length === 0 ? (
                <div className="py-10 text-center">
                    <FolderTree className="h-8 w-8 text-gray-300 mx-auto mb-2" />
                    <p className="text-sm text-gray-500">No shared folders set up yet.</p>
                    <p className="text-xs text-gray-400 mt-1">
                        An admin can add your department's folders in Admin&nbsp;→&nbsp;Folders.
                    </p>
                </div>
            ) : (
                <ul className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {folders.map(f => (
                        <li key={f.id}>
                            <a
                                href={driveUrl(f)}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="group flex items-center gap-3 border border-gray-200 rounded-lg px-3 py-2.5 hover:border-brand hover:bg-rose-50/40 transition-colors"
                            >
                                <FolderTree className="h-5 w-5 text-brand flex-shrink-0" />
                                <div className="min-w-0 flex-1">
                                    <div className="text-sm font-medium text-gray-900 truncate">
                                        {f.folder_name}
                                    </div>
                                    <div className="text-[11px] text-gray-400">
                                        {f.department ? f.department : 'Company-wide'}
                                    </div>
                                </div>
                                <ExternalLink className="h-4 w-4 text-gray-300 group-hover:text-brand flex-shrink-0" />
                            </a>
                        </li>
                    ))}
                </ul>
            )}
        </div>
    );
};

export default DepartmentFoldersColumn;
