// Shared briefing (Email + Calendar) used by Homefeed (Today strip summary)
// and Productivity (full Email + Calendar views). UC-39. Both read one cached
// copy (productivityCache.ts), so opening Productivity after Home is instant.
import type { Briefing } from './types';
import { briefingResource, useCachedResource } from './productivityCache';


export interface UseBriefingResult {
    briefing: Briefing | null;
    loading: boolean;
    refreshing: boolean;
    error: string | null;
    refresh: () => Promise<void>;
}


export function useBriefing(opts?: { load?: boolean }): UseBriefingResult {
    const { data, loading, refreshing, error, refresh } = useCachedResource(briefingResource, opts);
    return { briefing: data, loading, refreshing, error, refresh };
}
