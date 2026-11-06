// The reads behind Productivity's Email + Calendar, Slack and Documents tabs,
// held in one shared in-memory cache (see cachedResource.ts).
//
// Before this, Home fetched the briefing and Slack snapshot for the Today strip
// and Productivity threw both away and fetched them again from zero. Now Home
// warms all three once AI Priorities has loaded (`warmProductivity`), and
// Productivity paints from the cache, refreshing in the background when the
// copy is over a minute old.
import { useCallback, useEffect, useSyncExternalStore } from 'react';
import client, { impersonationHeaders } from '../../api/client';
import { activeToken } from '../../brand/storage';
import { createCachedResource, type CachedResource } from './cachedResource';
import type { Briefing, DriveActivityResponse } from './types';
import type { SlackActivity } from './useSlackActivity';

// Long enough to cover Home → Productivity and tab hopping without a refetch,
// short enough that a copy you come back to later is refreshed on sight.
const FRESH_MS = 60_000;

// The token and impersonation context every request carries. A different value
// means a different person, so their copy is never shown.
function identity(): string {
    return `${activeToken() ?? ''}|${JSON.stringify(impersonationHeaders())}`;
}

export const briefingResource = createCachedResource<Briefing>(
    () => client.post<Briefing>('/api/homefeed/generate').then(res => res.data),
    { identity, freshMs: FRESH_MS, fallbackError: 'Failed to generate briefing' },
);

export const slackActivityResource = createCachedResource<SlackActivity>(
    () => client.get<SlackActivity>('/api/slack/activity').then(res => res.data),
    { identity, freshMs: FRESH_MS, fallbackError: 'Failed to load Slack activity' },
);

// Productivity → Documents. The tab's own default query; the folder picker
// narrows it server-side, so a folder change is a forced refresh of this.
export const driveActivityResource = createCachedResource<DriveActivityResponse>(
    () =>
        client
            .get<DriveActivityResponse>('/api/drive/recent-activity', {
                params: { days_back: 7, max_results: 25 },
            })
            .then(res => res.data),
    { identity, freshMs: FRESH_MS, fallbackError: 'Failed to load Drive activity' },
);

// Get the Productivity tabs ready in the background. Safe to call often: each
// read is skipped while its copy is fresh and joined while it is in flight.
export function warmProductivity(): void {
    void briefingResource.load();
    void slackActivityResource.load();
    void driveActivityResource.load();
}

export interface CachedRead<T> {
    data: T | null;
    // Nothing to show yet. A cached copy being refreshed is `refreshing` instead.
    loading: boolean;
    refreshing: boolean;
    error: string | null;
    refresh: () => Promise<void>;
}

// `load: false` subscribes without fetching, for a screen that wants the data
// when it arrives but leaves the timing to `warmProductivity`.
export function useCachedResource<T>(
    resource: CachedResource<T>,
    { load = true }: { load?: boolean } = {},
): CachedRead<T> {
    const state = useSyncExternalStore(resource.subscribe, resource.getSnapshot);
    useEffect(() => {
        if (load) void resource.load();
    }, [resource, load]);
    const refresh = useCallback(() => resource.load({ force: true }), [resource]);
    return {
        data: state.data,
        // Before the first load starts, a screen that asked for one is loading,
        // not empty — otherwise it flashes "all caught up" for one frame.
        loading: state.data === null && (state.fetching || (load && state.error === null)),
        refreshing: state.fetching,
        error: state.error,
        refresh,
    };
}
