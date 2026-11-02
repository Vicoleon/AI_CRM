import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, AppWindow, CheckCircle2, Loader2, Plug } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { clearReturnTo } from '../components/whatsNew/returnTo';
import {
    browserExtensionError,
    browserLabel,
    connectBrowser,
    disconnectBrowser,
    handToExtension,
    readInstalledExtension,
} from '../api/browserExtension';
import type { ConnectResponse, InstalledExtension } from '../api/browserExtension';

/**
 * Connect Cubie for Chrome (/chrome/connect).
 *
 * The extension opens this page when it has no connection (or a dead one).
 * It mints a key for this browser and hands it to the extension in the page,
 * so the person never copies anything. Three states:
 *
 *   * The extension is not installed: how to install it (from the package the
 *     Cubie admin hands out, until the company installs it from the Chrome
 *     Web Store).
 *   * It is installed: a short consent card and "Connect this browser".
 *   * Connected, or why not and what to do about it.
 *
 * The key is in a local variable for the length of one click: posted to the
 * extension, then dropped. It never goes into state, the URL or a log, and a
 * key the extension did not confirm is revoked straight away, so a failed
 * handshake never leaves a live key nobody holds.
 */

type Phase =
    | { kind: 'idle' }
    | { kind: 'connecting' }
    | { kind: 'connected'; firstName?: string }
    | { kind: 'failed'; message: string; detail?: string; advice: string };

/** What to do when Cubie could not make the connection. */
const RELOAD_ADVICE = 'Reload this page and try again.';

/** What to do when the extension did not take the key. */
const EXTENSION_ADVICE =
    'Reload this page and try again. If it keeps failing, open chrome://extensions, find Cubie for Chrome and press its reload arrow, then come back here.';

/** The content script marks the page at document_start; this re-checks once in
 *  case Chrome injected it a moment after the page loaded. */
const RECHECK_MS = 500;

export const ChromeConnect: React.FC = () => {
    const { user } = useAuth();
    const [extension, setExtension] = useState<InstalledExtension | null>(() => readInstalledExtension());
    const [rechecked, setRechecked] = useState(false);
    const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
    // Stops waiting for the extension's answer if the person leaves the page.
    const handshake = useRef<AbortController | null>(null);

    useEffect(() => {
        // Arrived: forget the post-sign-in return trip that brought us here.
        clearReturnTo();
        const timer = window.setTimeout(() => {
            setExtension(readInstalledExtension());
            setRechecked(true);
        }, RECHECK_MS);
        return () => {
            window.clearTimeout(timer);
            handshake.current?.abort();
        };
    }, []);

    const connect = async () => {
        if (!extension || phase.kind === 'connecting') return;
        setPhase({ kind: 'connecting' });
        // Read again now: the extension fills in the connection it holds a
        // moment after it marks the page, and that is the key to retire.
        const held = (readInstalledExtension() ?? extension).connectionId;
        let minted: ConnectResponse | null;
        try {
            minted = await connectBrowser({
                label: browserLabel(),
                replaces: held || undefined,
            });
        } catch (e) {
            setPhase({
                kind: 'failed',
                message: browserExtensionError(e, 'Cubie could not connect this browser.'),
                advice: RELOAD_ADVICE,
            });
            return;
        }
        const connectionId = minted.connection.id;
        const controller = new AbortController();
        handshake.current = controller;
        const pending = handToExtension(minted.token, connectionId, { signal: controller.signal });
        minted = null; // posted: the extension holds the key now, this page keeps no copy
        const answer = await pending;
        handshake.current = null;
        if (controller.signal.aborted) return; // the page is gone
        if (answer?.ok) {
            setPhase({ kind: 'connected', firstName: answer.firstName });
            return;
        }
        // Not confirmed: revoke the key rather than leave it live and unheld.
        void disconnectBrowser(connectionId).catch(() => { /* it lapses on its own */ });
        setPhase({
            kind: 'failed',
            message: answer
                ? 'Cubie for Chrome could not save the connection.'
                : 'Cubie for Chrome did not answer.',
            detail: answer?.error,
            advice: EXTENSION_ADVICE,
        });
    };

    const checking = !extension && !rechecked;

    return (
        <div className="max-w-2xl mx-auto px-4 py-8">
            <div className="flex items-center gap-3 mb-1">
                <AppWindow className="text-brand" size={26} />
                <h1 className="text-2xl font-bold text-gray-900">Cubie for Chrome</h1>
            </div>
            <p className="text-gray-500 mb-6">
                Your promises past due and today&apos;s priorities, on every new tab in Chrome.
            </p>

            <div data-tour="chrome-connect-card" className="bg-white border border-gray-200 rounded-xl shadow-sm p-6">
                {checking ? (
                    <div className="flex items-center gap-2 text-gray-500 text-sm">
                        <Loader2 className="animate-spin" size={16} /> Looking for Cubie for Chrome in this browser…
                    </div>
                ) : !extension ? (
                    <InstallSteps />
                ) : phase.kind === 'connected' ? (
                    <Connected firstName={phase.firstName} />
                ) : user?.impersonation ? (
                    <p className="text-sm text-gray-700">
                        You are viewing Cubie as {user.impersonation.target_name}. Stop viewing as
                        them to connect this browser to your own account.
                    </p>
                ) : (
                    <Consent
                        busy={phase.kind === 'connecting'}
                        failure={phase.kind === 'failed' ? phase : null}
                        onConnect={connect}
                    />
                )}
            </div>
        </div>
    );
};

const InstallSteps: React.FC = () => (
    <>
        <h2 className="text-lg font-semibold text-gray-900">First, add Cubie for Chrome to this browser</h2>
        <p className="text-sm text-gray-600 mt-1">
            Cubie could not find the extension in this browser. Once your company installs it
            from the Chrome Web Store, it arrives on its own. Until then it is added by hand, which
            takes about a minute.
        </p>
        <ol className="list-decimal pl-5 mt-4 space-y-2 text-sm text-gray-800">
            <li>
                Ask your Cubie admin for the Cubie for Chrome package, built for your Cubie address,
                and unzip it.
            </li>
            <li>
                In Chrome&apos;s address bar, type <span className="font-mono text-[13px]">chrome://extensions</span>{' '}
                and press Enter.
            </li>
            <li>Turn on <span className="font-medium">Developer mode</span>, at the top right.</li>
            <li>
                Click <span className="font-medium">Load unpacked</span> and pick the folder you
                unzipped.
            </li>
            <li>Come back to this page and reload it.</li>
        </ol>
        <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-5 flex items-center gap-2 px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 transition-colors"
        >
            Reload this page
        </button>
    </>
);

const Consent: React.FC<{
    busy: boolean;
    failure: { message: string; detail?: string; advice: string } | null;
    onConnect: () => void;
}> = ({ busy, failure, onConnect }) => (
    <>
        <h2 className="text-lg font-semibold text-gray-900">Connect Cubie for Chrome to your account</h2>
        <dl className="mt-4 space-y-3 text-sm">
            <div>
                <dt className="font-medium text-gray-900">What it shows</dt>
                <dd className="text-gray-600">
                    Your promises past due, then today&apos;s priorities, each time you open a new tab.
                </dd>
            </div>
            <div>
                <dt className="font-medium text-gray-900">What it can do</dt>
                <dd className="text-gray-600">Open Cubie, and tick off a promise or a priority.</dd>
            </div>
            <div>
                <dt className="font-medium text-gray-900">What it can&apos;t do</dt>
                <dd className="text-gray-600">
                    It cannot read your email, your browsing or any other site.
                </dd>
            </div>
        </dl>
        <p className="text-xs text-gray-500 mt-4">
            You can disconnect it at any time in{' '}
            <Link to="/settings?tab=general" className="text-brand hover:underline">Settings → General</Link>.
        </p>

        {failure && (
            <div className="mt-4 flex items-start gap-2 p-3 rounded-md bg-red-50 border border-red-200 text-sm text-red-800">
                <AlertCircle size={16} className="flex-shrink-0 mt-0.5" />
                <div>
                    <p className="font-medium">{failure.message}</p>
                    {failure.detail && <p className="mt-0.5">{failure.detail}</p>}
                    <p className="mt-1">{failure.advice}</p>
                </div>
            </div>
        )}

        <button
            type="button"
            onClick={onConnect}
            disabled={busy}
            className="mt-5 flex items-center gap-2 px-4 py-2 bg-brand text-white rounded-md text-sm font-medium hover:bg-brand-hover disabled:opacity-50 transition-colors"
        >
            {busy ? <Loader2 className="animate-spin" size={14} /> : <Plug size={14} />}
            Connect this browser
        </button>
    </>
);

const Connected: React.FC<{ firstName?: string }> = ({ firstName }) => (
    <div className="flex items-start gap-3">
        <CheckCircle2 className="text-green-600 flex-shrink-0 mt-0.5" size={22} />
        <div>
            <h2 className="text-lg font-semibold text-gray-900">
                Connected{firstName ? `, ${firstName}` : ''}.
            </h2>
            <p className="text-sm text-gray-600 mt-1">Open a new tab to see your day.</p>
            <p className="text-xs text-gray-500 mt-3">
                This browser is listed in{' '}
                <Link to="/settings?tab=general" className="text-brand hover:underline">Settings → General</Link>,
                where you can disconnect it.
            </p>
        </div>
    </div>
);
