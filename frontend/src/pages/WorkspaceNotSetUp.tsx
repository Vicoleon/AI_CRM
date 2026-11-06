import React from 'react';
import { googleLogout } from '@react-oauth/google';
import { Link, useSearchParams } from 'react-router-dom';
import { PRODUCT_NAME, VENDOR_NAME, cubeMark } from '../brand/brand';

const REASONS = {
    workspace_unknown: {
        title: 'Your workspace is not set up yet',
        guidance: 'Ask your company administrator to set up your workspace in Cubie.',
    },
    workspace_personal: {
        title: 'Use your company account',
        guidance: 'Sign in with your company Google Workspace account to reach your workspace.',
    },
    workspace_suspended: {
        title: 'This workspace is paused',
        guidance: 'Ask your company administrator when access will be restored.',
    },
} as const;

export const WorkspaceNotSetUp: React.FC = () => {
    const [params] = useSearchParams();
    const rawReason = params.get('reason');
    const reason = rawReason && Object.hasOwn(REASONS, rawReason)
        ? REASONS[rawReason as keyof typeof REASONS]
        : REASONS.workspace_unknown;

    return (
        <main className="flex min-h-screen items-center justify-center bg-gray-50 px-4">
            <div className="w-full max-w-md rounded-2xl bg-white p-8 text-center shadow-xl">
                <div className="mb-7 flex items-center justify-center gap-3">
                    <img src={cubeMark} alt="" className="h-12 w-12" />
                    <div className="text-left">
                        <p className="font-display text-2xl font-semibold leading-none text-ink">{PRODUCT_NAME}</p>
                        <p className="mt-1 text-xs font-medium text-gray-500">by {VENDOR_NAME}</p>
                    </div>
                </div>
                <h1 className="font-display text-xl font-semibold text-ink">{reason.title}</h1>
                <p className="mt-3 text-sm leading-relaxed text-gray-600">{reason.guidance}</p>
                <Link
                    to="/login"
                    onClick={() => googleLogout()}
                    className="mt-7 inline-flex min-h-11 items-center justify-center rounded-lg bg-brand px-5 py-2 text-sm font-semibold text-white hover:opacity-90"
                >
                    Use another account
                </Link>
            </div>
        </main>
    );
};
