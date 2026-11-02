// The Cubie side panel in Google Meet: connect to Meet, sign in, show this
// call's checklist. Each step has its own screen and its own sentence, because
// the AE reading them is on a live client call and cannot debug anything.
//
// Two things here are deliberate:
//
// 1. `loading` IS DERIVED, never stored: a settled result is keyed by the load
//    it answered (the same pattern as `pages/calls/StartCall.tsx`). A
//    `setLoading(true)` inside an effect keyed on a param is an ERROR under
//    `react-hooks/set-state-in-effect`.
// 2. A FAILED REFRESH KEEPS THE CHECKLIST ON SCREEN. Losing the questions
//    mid-call because a refresh hiccupped would be worse than a stale list, so
//    the last good answer stays and the failure is said above it.
import React, { useCallback, useEffect, useState } from 'react';
import { GoogleLogin, useGoogleLogin } from '@react-oauth/google';
import { ClipboardList, LogIn, RefreshCw, TriangleAlert } from 'lucide-react';
import {
    AddonLoadError,
    clearToken,
    exchangeForSession,
    fetchAddonContext,
    readToken,
    storeToken,
} from './addonApi';
import { CallChecklist } from './CallChecklist';
import { connectToMeet, type MeetConnection } from './meetSession';
import {
    addProduct,
    EMPTY_LOCAL,
    localStateKey,
    parseLocalState,
    removeProduct,
    toggleTick,
    type AddonContext,
    type LoadFailure,
    type LocalState,
} from './panelState';

/** Browser-local YYYY-MM-DD — the day the ticks belong to. */
const localDay = (): string => new Date().toLocaleDateString('en-CA');

function readLocal(code: string): LocalState {
    try {
        return parseLocalState(window.localStorage.getItem(localStateKey(code, localDay())));
    } catch {
        return EMPTY_LOCAL;
    }
}

function writeLocal(code: string, state: LocalState): void {
    try {
        window.localStorage.setItem(localStateKey(code, localDay()), JSON.stringify(state));
    } catch {
        // Storage refused inside Meet's iframe: the ticks last as long as the panel.
    }
}

const BUTTON =
    'inline-flex items-center justify-center gap-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-brand-hover focus:outline-none focus:ring-2 focus:ring-brand focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60';
const GHOST =
    'inline-flex items-center justify-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm hover:bg-slate-50';

function Card({ title, children }: { title: string; children?: React.ReactNode }) {
    return (
        <div className="flex min-h-screen items-start justify-center bg-slate-50 px-4 py-8">
            <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-5 text-center shadow-sm">
                <img src="/cubie.svg" alt="" className="mx-auto h-9 w-9" />
                <p className="mt-3 text-sm font-semibold text-slate-900">{title}</p>
                <div className="mt-2 space-y-3 text-xs leading-relaxed text-slate-600">{children}</div>
            </div>
        </div>
    );
}

function Waiting({ label }: { label: string }) {
    return (
        <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
            <p className="flex items-center gap-2 text-sm text-slate-500">
                <RefreshCw size={14} className="animate-spin" /> {label}
            </p>
        </div>
    );
}

function SignIn({ onSignedIn }: { onSignedIn: (token: string) => void }) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const useAccessTokenExchange = import.meta.env.VITE_MEET_ADDON_ACCESS_TOKEN_EXCHANGE === 'true';

    const finishSignIn = async (googleCredential?: string) => {
        if (!googleCredential) {
            setError('Google sign-in did not complete. Please try again.');
            return;
        }
        setBusy(true);
        setError(null);
        try {
            const token = await exchangeForSession(googleCredential);
            storeToken(token);
            onSignedIn(token);
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : 'Sign-in failed. Please try again.');
        } finally {
            setBusy(false);
        }
    };

    // Some Meet iframe environments may still need the one-time access-token
    // exchange. It is disabled unless explicitly enabled for this client build.
    const accessTokenLogin = useGoogleLogin({
        onSuccess: response => { void finishSignIn(response.access_token); },
        onError: () => setError('Google sign-in did not complete. Please try again.'),
        onNonOAuthError: err =>
            setError(
                err.type === 'popup_failed_to_open'
                    ? 'Your browser blocked the Google sign-in window. Allow pop-ups for meet.google.com, then try again.'
                    : 'The Google sign-in window closed before it finished. Please try again.',
            ),
    });

    return (
        <Card title="Sign in to see your call checklist">
            <p>
                Cubie shows the questions this client call has to cover, for the products you
                picked. Signing in here is separate from your Cubie tab — Meet keeps the two
                apart — and keeps you signed in for a few hours.
            </p>
            {busy ? <p>Signing in…</p> : useAccessTokenExchange ? (
                <button type="button" onClick={() => accessTokenLogin()} className={BUTTON}>
                    <LogIn size={15} /> Sign in with Google
                </button>
            ) : (
                <div className="flex justify-center">
                    <GoogleLogin
                        onSuccess={response => { void finishSignIn(response.credential); }}
                        onError={() => setError('Google sign-in did not complete. Please try again.')}
                        text="signin_with"
                    />
                </div>
            )}
            {error && (
                <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-left text-red-800">
                    {error}
                </p>
            )}
        </Card>
    );
}

function FailureCard({ failure, onRetry }: { failure: LoadFailure; onRetry: () => void }) {
    switch (failure.kind) {
        case 'not_for_you':
            return (
                <Card title="Not on for your account yet">
                    <p>
                        {failure.message} Nothing is wrong with your account. If you take client
                        calls and this should include you, ask a Cubie administrator.
                    </p>
                </Card>
            );
        case 'switched_off':
            return (
                <Card title="Cubie in Google Meet is not switched on yet">
                    <p>Nothing is wrong with your account. Run the call the way you do today.</p>
                </Card>
            );
        case 'bad_code':
            return (
                <Card title="This is not a call Cubie can read">
                    <p>Meet did not hand over a meeting code Cubie recognises. Close the panel and open it again from Activities.</p>
                </Card>
            );
        case 'signed_out':
            // Handled by the parent (back to sign-in); never rendered here.
            return null;
        default:
            return (
                <Card title="The checklist did not load">
                    <p>{failure.message}</p>
                    <button type="button" onClick={onRetry} className={GHOST}>
                        <RefreshCw size={13} /> Try again
                    </button>
                </Card>
            );
    }
}

interface Settled {
    key: number;
    ctx: AddonContext | null;
    failure: LoadFailure | null;
}

function CallPanel({
    code,
    token,
    standalone,
    onSignedOut,
}: {
    code: string;
    token: string;
    standalone: boolean;
    onSignedOut: () => void;
}) {
    const [reloadKey, setReloadKey] = useState(0);
    const [settled, setSettled] = useState<Settled | null>(null);
    const [local, setLocal] = useState<LocalState>(() => readLocal(code));

    useEffect(() => {
        let cancelled = false;
        fetchAddonContext(code, token)
            .then(ctx => {
                if (!cancelled) setSettled({ key: reloadKey, ctx, failure: null });
            })
            .catch((error: unknown) => {
                if (cancelled) return;
                const failure: LoadFailure =
                    error instanceof AddonLoadError
                        ? error.failure
                        : {
                              kind: 'error',
                              message:
                                  error instanceof Error
                                      ? error.message
                                      : 'Cubie could not load this call’s checklist.',
                          };
                if (failure.kind === 'signed_out') {
                    onSignedOut();
                    return;
                }
                // Note 2 — keep the last good checklist through a failed refresh.
                setSettled(prev => ({ key: reloadKey, ctx: prev?.ctx ?? null, failure }));
            });
        return () => {
            cancelled = true;
        };
    }, [code, token, reloadKey, onSignedOut]);

    const refresh = () => setReloadKey(k => k + 1);
    const update = (next: LocalState) => {
        setLocal(next);
        writeLocal(code, next);
    };

    if (settled === null) return <Waiting label="Reading your calendar…" />;
    const refreshing = settled.key !== reloadKey; // note 1 — derived
    if (!settled.ctx) {
        if (refreshing) return <Waiting label="Trying again…" />;
        return settled.failure ? <FailureCard failure={settled.failure} onRetry={refresh} /> : null;
    }

    return (
        <>
            {standalone && (
                <p className="bg-amber-100 px-4 py-1.5 text-[11px] text-amber-900">
                    Opened outside Meet, for meeting {code}. In a call, the panel finds the code itself.
                </p>
            )}
            {settled.failure && !refreshing && (
                <p className="flex items-start gap-1.5 bg-red-50 px-4 py-2 text-[11px] text-red-800">
                    <TriangleAlert size={12} className="mt-0.5 flex-shrink-0" />
                    Refresh failed, so this is the checklist from before.{' '}
                    {'message' in settled.failure ? settled.failure.message : ''}
                </p>
            )}
            <CallChecklist
                ctx={settled.ctx}
                local={local}
                refreshing={refreshing}
                onRefresh={refresh}
                onToggle={key => update(toggleTick(local, key))}
                onAdd={productKey => update(addProduct(local, productKey))}
                onRemove={productKey => update(removeProduct(local, productKey))}
            />
        </>
    );
}

export const MeetAddonApp: React.FC = () => {
    const [connection, setConnection] = useState<MeetConnection | null>(null);
    const [token, setToken] = useState<string | null>(() => readToken());

    useEffect(() => {
        let cancelled = false;
        void connectToMeet().then(result => {
            if (!cancelled) setConnection(result);
        });
        return () => {
            cancelled = true;
        };
    }, []);

    // Stable, so the panel's load effect does not re-run on every render.
    const signOut = useCallback(() => {
        clearToken();
        setToken(null);
    }, []);

    if (connection === null) return <Waiting label="Connecting to Meet…" />;

    if (connection.kind === 'failed') {
        return (
            <Card title={connection.message}>
                <p>Close the Cubie panel and open it again from Activities. If it keeps happening, tell your Cubie administrator.</p>
                {connection.detail && <p className="font-mono text-[10px] text-slate-400">{connection.detail}</p>}
            </Card>
        );
    }

    if (connection.kind === 'standalone' && !connection.meetingCode) {
        return (
            <Card title="Open this from Google Meet">
                <p className="flex items-start gap-2 text-left">
                    <ClipboardList size={14} className="mt-0.5 flex-shrink-0 text-brand" />
                    <span>
                        This is Cubie&apos;s call checklist for Google Meet. In a call, open{' '}
                        <span className="font-medium">Activities</span> (the shapes icon, bottom
                        right) and choose <span className="font-medium">Cubie</span>.
                    </span>
                </p>
            </Card>
        );
    }

    const code = connection.meetingCode as string;
    if (!token) return <SignIn onSignedIn={setToken} />;
    return (
        <CallPanel
            key={code}
            code={code}
            token={token}
            standalone={connection.kind === 'standalone'}
            onSignedOut={signOut}
        />
    );
};

export default MeetAddonApp;
