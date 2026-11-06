// Page tours ("Walk me"): each person's memory of the tours they have seen.
// The server half is backend/routers/tours.py; the tours themselves live in
// src/tours/.
import client from './client';
import type { SeenMap, SeenStatus } from '../tours/logic.ts';

export interface TourState {
    /** False when the person turned off tours that start by themselves. */
    enabled: boolean;
    seen: SeenMap;
}

export async function fetchTourState(): Promise<TourState> {
    const res = await client.get<TourState>('/api/tours');
    return res.data;
}

export async function markToursSeen(tourIds: string[], status: SeenStatus): Promise<TourState> {
    const res = await client.post<TourState>('/api/tours/seen', { tour_ids: tourIds, status });
    return res.data;
}

export async function setToursEnabled(enabled: boolean): Promise<TourState> {
    const res = await client.put<TourState>('/api/tours/settings', { enabled });
    return res.data;
}

/** Every page's tour runs again. Leaves the on/off switch as it is. */
export async function forgetToursSeen(): Promise<TourState> {
    const res = await client.delete<TourState>('/api/tours/seen');
    return res.data;
}
