// The two hooks every Insights page shares: the filters (read from the URL,
// written back with `replace`, remembered between visits) and the `/meta`
// catalog (fetched once and shared).
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { fetchInsightsMeta, insightsError } from '../../api/insights';
import type { InsightsError, InsightsMeta } from '../../api/insights';
import {
    DEFAULT_FILTERS,
    filtersKey,
    filtersToQuery,
    hasFilterParams,
    mergeFilterParams,
    parseFilters,
    rawFilterQuery,
    rememberFilters,
    restoreFilters,
} from './insightsFilters';
import type { InsightsFilters } from './insightsFilters';

/**
 * The filters this page is showing.
 *
 * - The URL is the source of truth. A URL with no filter params at all (a bare
 *   nav link, a first visit) gets the viewer's remembered filters, or the
 *   defaults, and the URL is then rewritten to say so — so what is on screen
 *   is always what a copied link reproduces.
 * - A URL with bad values (an unknown range, an inverted custom range) is
 *   rewritten to what the page actually shows.
 * - `filters` keeps its identity while the filter VALUES are unchanged, so a
 *   fetch keyed on it does not re-run when unrelated params move.
 */
export function useInsightsFilters() {
    const [params, setParams] = useSearchParams();
    const urlHasFilters = hasFilterParams(params);
    const effective = urlHasFilters ? parseFilters(params) : (restoreFilters() ?? DEFAULT_FILTERS);
    const key = filtersKey(effective);
    const filters = useMemo<InsightsFilters>(() => parseFilters(key), [key]);
    const raw = rawFilterQuery(params);

    useEffect(() => {
        if (raw !== key) setParams(prev => mergeFilterParams(prev, filters), { replace: true });
    }, [raw, key, filters, setParams]);

    useEffect(() => {
        if (urlHasFilters) rememberFilters(filters);
    }, [filters, urlHasFilters]);

    const setFilters = useCallback(
        (next: InsightsFilters) => setParams(prev => mergeFilterParams(prev, next), { replace: true }),
        [setParams],
    );

    return {
        filters,
        setFilters,
        /** `?dept=…&range=…` for carrying the filters onto another Insights link. */
        filterQuery: filtersToQuery(filters),
    };
}

/** `/meta`, shared across pages (the API module caches it per identity). */
export function useInsightsMeta() {
    const [state, setState] = useState<{ meta: InsightsMeta | null; error: InsightsError | null } | null>(null);
    const [attempt, setAttempt] = useState(0);

    // Every setState runs in the async callback, never in the effect body
    // (react-hooks/set-state-in-effect); "loading" is derived from `state`.
    useEffect(() => {
        let cancelled = false;
        fetchInsightsMeta({ force: attempt > 0 })
            .then(meta => {
                if (!cancelled) setState({ meta, error: null });
            })
            .catch(e => {
                if (!cancelled) setState({ meta: null, error: insightsError(e) });
            });
        return () => {
            cancelled = true;
        };
    }, [attempt]);

    return {
        meta: state?.meta ?? null,
        error: state?.error ?? null,
        loading: state === null,
        retry: () => setAttempt(a => a + 1),
    };
}
