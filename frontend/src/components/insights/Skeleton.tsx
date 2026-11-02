import React from 'react';

const PULSE = 'animate-pulse rounded-md bg-gray-200/70 motion-reduce:animate-none';

/** A block the shape of what is loading. */
export const SkeletonBlock: React.FC<{ className?: string }> = ({ className = '' }) => (
    <div className={`${PULSE} ${className}`} aria-hidden="true" />
);

const SkeletonCard: React.FC<{ span: string; height: string }> = ({ span, height }) => (
    <div className={`${span} rounded-lg border border-gray-200 bg-white p-4 shadow-sm`} aria-hidden="true">
        <SkeletonBlock className="h-4 w-40" />
        <SkeletonBlock className="mt-2 h-3 w-64 max-w-full" />
        <SkeletonBlock className={`mt-4 w-full ${height}`} />
    </div>
);

/** A dashboard's first load: the KPI row, two half cards and a full one. */
export const DashboardSkeleton: React.FC<{ tiles?: number }> = ({ tiles = 5 }) => (
    <div role="status" aria-label="Loading the dashboard" className="space-y-5">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
            {Array.from({ length: tiles }, (_, i) => (
                <div key={i} className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm" aria-hidden="true">
                    <SkeletonBlock className="h-3 w-24" />
                    <SkeletonBlock className="mt-3 h-7 w-20" />
                    <SkeletonBlock className="mt-2 h-3 w-16" />
                    <SkeletonBlock className="mt-3 h-6 w-full" />
                </div>
            ))}
        </div>
        <div className="grid grid-cols-12 gap-4">
            <SkeletonCard span="col-span-12 lg:col-span-6" height="h-56" />
            <SkeletonCard span="col-span-12 lg:col-span-6" height="h-56" />
            <SkeletonCard span="col-span-12" height="h-64" />
        </div>
        <span className="sr-only">Loading…</span>
    </div>
);

/** The person profile's first load. */
export const ProfileSkeleton: React.FC = () => (
    <div role="status" aria-label="Loading the profile" className="space-y-5">
        <div className="flex items-center gap-4 rounded-lg border border-gray-200 bg-white p-5 shadow-sm" aria-hidden="true">
            <SkeletonBlock className="h-14 w-14 rounded-full" />
            <div className="flex-1">
                <SkeletonBlock className="h-5 w-48" />
                <SkeletonBlock className="mt-2 h-3 w-64 max-w-full" />
            </div>
        </div>
        <SkeletonCard span="" height="h-32" />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {Array.from({ length: 4 }, (_, i) => (
                <SkeletonBlock key={i} className="h-28" />
            ))}
        </div>
        <span className="sr-only">Loading…</span>
    </div>
);
