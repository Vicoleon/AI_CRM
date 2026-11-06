import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, Loader2, Plug, Unplug } from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import { formatInstant, formatInstantDate } from '../../utils/datetime';
import {
    browserExtensionError,
    disconnectBrowser,
    listConnections,
    readInstalledExtension,
} from '../../api/browserExtension';
import type { BrowserConnection } from '../../api/browserExtension';

/**
 * Settings → General → Cubie for Chrome: the browsers connected to this
 * person's account, each with Disconnect, and the way to connect another.
 * Rendered only when the server lets the reader use the feature
 * (`chrome_extension`); the list endpoint re-checks.
 *
 * Disconnecting kills that browser's key at once: its next refresh gets
 * "connect again" and its new tab stops showing anything. Connections also
 * lapse on their own after 90 days, and offboarding removes them all.
 */
export const CubieForChromeCard: React.FC = () => {
    const { user } = useAuth();
    const [rows, setRows] = useState<BrowserConnection[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busyId, setBusyId] = useState<string | null>(null);
    // The connection this very browser holds, from the extension's mark on the
    // page, so its row can say "This browser".
    const [thisBrowser] = useState(() => readInstalledExtension()?.connectionId ?? '');
    // Viewing Cubie as someone else is read-only: the server refuses a
    // disconnect, so the button is not offered.
    const readOnly = Boolean(user?.impersonation);

    useEffect(() => {
        let cancelled = false;
        listConnections()
            .then(list => { if (!cancelled) setRows(list); })
            .catch(e => { if (!cancelled) setError(browserExtensionError(e, 'Cubie could not load your connected browsers.')); });
        return () => { cancelled = true; };
    }, []);

    const disconnect = async (row: BrowserConnection) => {
        if (!window.confirm(`Disconnect ${row.label}? Its new tab stops showing your day until you connect it again.`)) return;
        setBusyId(row.id);
        setError(null);
        try {
            await disconnectBrowser(row.id);
            setRows(await listConnections());
        } catch (e) {
            setError(browserExtensionError(e, 'Cubie could not disconnect that browser. Try again.'));
        } finally {
            setBusyId(null);
        }
    };

    return (
        <>
            <h3 className="text-sm font-semibold text-gray-900 mt-8 mb-2">Cubie for Chrome</h3>
            <div data-tour="settings-chrome-extension" className="bg-white rounded-lg border border-gray-200 divide-y divide-gray-100">
                <p className="p-5 text-xs text-gray-500">
                    Your promises past due and today&apos;s priorities on every new tab in Chrome.
                    These are the browsers connected to your account. A connection lapses after 90
                    days; connect the browser again to keep it going.
                </p>

                {error && (
                    <div className="p-5">
                        <div className="flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200 text-sm text-red-800">
                            <AlertCircle size={16} className="flex-shrink-0 mt-0.5" />
                            <span>{error}</span>
                        </div>
                    </div>
                )}

                {rows === null && !error && (
                    <div className="p-5 flex items-center gap-2 text-sm text-gray-500">
                        <Loader2 className="animate-spin" size={14} /> Loading…
                    </div>
                )}

                {rows !== null && rows.length === 0 && (
                    <p className="p-5 text-sm text-gray-600">No browser is connected yet.</p>
                )}

                {rows?.map(row => (
                    <div key={row.id} className="p-5 flex items-center justify-between gap-4">
                        <div className="min-w-0">
                            <p className="text-sm font-medium text-gray-900 flex items-center gap-2">
                                {row.label}
                                <span className="font-mono text-xs font-normal text-gray-400">…{row.hint}</span>
                                {thisBrowser === row.id && (
                                    <span className="text-[11px] font-normal bg-brand-tint text-brand-hover rounded-full px-2 py-0.5">
                                        This browser
                                    </span>
                                )}
                            </p>
                            <p className="text-xs text-gray-500 mt-0.5">
                                Connected {formatInstantDate(row.created_at)}
                                {' · '}
                                {row.last_used_at
                                    ? `last used ${formatInstant(row.last_used_at, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}`
                                    : 'not used yet'}
                            </p>
                        </div>
                        {!readOnly && (
                            <button
                                type="button"
                                onClick={() => disconnect(row)}
                                disabled={busyId !== null}
                                className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-300 text-gray-700 rounded-md text-xs font-medium hover:bg-gray-50 disabled:opacity-50 transition-colors flex-shrink-0"
                            >
                                {busyId === row.id ? <Loader2 className="animate-spin" size={12} /> : <Unplug size={12} />}
                                Disconnect
                            </button>
                        )}
                    </div>
                ))}

                <div className="p-5">
                    <Link
                        to="/chrome/connect"
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-brand hover:underline"
                    >
                        <Plug size={14} /> Connect this browser
                    </Link>
                </div>
            </div>
        </>
    );
};
