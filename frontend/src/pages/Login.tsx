import React, { useState } from 'react';
import { GoogleLogin } from '@react-oauth/google';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { exchangeGoogleCredential, SessionExchangeError } from '../auth/session';

import { motion } from 'framer-motion';
import { PRODUCT_NAME, VENDOR_NAME, cubeMark } from '../brand/brand';

export const Login: React.FC = () => {
    const { setToken } = useAuth();
    const navigate = useNavigate();
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const onGoogleCredential = async (credential?: string) => {
        if (!credential) {
            setError('Google sign-in did not complete. Please try again.');
            return;
        }
        setIsLoading(true);
        setError(null);
        try {
            // GIS supplies an ID token; the server verifies it before routing.
            setToken(await exchangeGoogleCredential(credential));
        } catch (cause) {
            if (cause instanceof SessionExchangeError && cause.reason) {
                navigate(`/not-set-up?reason=${cause.reason}`, { replace: true });
            } else {
                setError(cause instanceof Error ? cause.message : 'Sign-in failed. Please try again.');
            }
        } finally {
            setIsLoading(false);
        }
    };

    const onLocalSignIn = async () => {
        setIsLoading(true);
        setError(null);
        try {
            // The server accepts this token only with local mock auth enabled.
            const response = await fetch('/api/users/me', {
                headers: { Authorization: 'Bearer mock-token' },
            });
            if (!response.ok) throw new Error('Local preview sign-in is unavailable.');
            setToken('mock-token');
        } catch (cause) {
            setError(cause instanceof Error ? cause.message : 'Local preview sign-in failed.');
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-gray-50">
            <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                className="bg-white p-8 rounded-2xl shadow-xl w-full max-w-md text-center"
            >
                <div className="mb-8">
                    {/* The platform lockup, always: the workspace is not known
                        until sign-in, so its own logo cannot show here yet. */}
                    <div className="flex items-center justify-center gap-3 mx-auto mb-6">
                        <img src={cubeMark} alt="" className="h-14 w-14" />
                        <div className="text-left">
                            <p className="font-display text-3xl font-semibold leading-none tracking-tight text-ink">
                                {PRODUCT_NAME}
                            </p>
                            <p className="mt-1 text-xs font-medium tracking-wide text-gray-500">by {VENDOR_NAME}</p>
                        </div>
                    </div>
                    <h1 className="font-display text-2xl font-semibold text-ink">Welcome to {PRODUCT_NAME}</h1>
                    <p className="text-gray-500 mt-2">Sign in to access your assistant</p>
                </div>

                <div className="flex flex-col items-center gap-4">
                    {/* The ID-token credential button needs a configured client ID. */}
                    {import.meta.env.VITE_GOOGLE_CLIENT_ID && import.meta.env.VITE_GOOGLE_CLIENT_ID !== 'place_holder_client_id' && (
                        <div className="flex min-h-11 w-full justify-center" aria-busy={isLoading}>
                            {isLoading
                                ? <p className="py-3 text-sm text-gray-600">Signing in…</p>
                                : <GoogleLogin
                                    onSuccess={response => { void onGoogleCredential(response.credential); }}
                                    onError={() => setError('Google sign-in did not complete. Please try again.')}
                                    text="signin_with"
                                />}
                        </div>
                    )}

                    {import.meta.env.DEV && (
                        <button
                            type="button"
                            onClick={() => { void onLocalSignIn(); }}
                            disabled={isLoading}
                            className="min-h-11 w-full rounded-xl bg-ink px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-ink/90 disabled:opacity-60"
                        >
                            {isLoading ? 'Signing in…' : 'Enter local preview'}
                        </button>
                    )}

                    {error && (
                        <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-2 w-full max-w-sm">
                            {error}
                        </p>
                    )}
                </div>
            </motion.div>
        </div>
    );
};
