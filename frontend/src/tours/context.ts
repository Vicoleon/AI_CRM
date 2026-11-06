// The page-tour context, and the hook the account menu and Settings read.
// Kept apart from TourProvider.tsx so that file exports only a component (the
// fast-refresh rule).
import { createContext, useContext } from 'react';

export interface ToursContextValue {
    /** The person is on the `page_tours` roster and not being viewed as by an admin. */
    available: boolean;
    /** Their memory of tours has loaded (until then nothing starts by itself). */
    loaded: boolean;
    /** Tours start by themselves (their own switch, on by default). */
    enabled: boolean;
    /** The page on screen has a tour "Replay this page's tour" can run. */
    pageHasTour: boolean;
    /** Run this page's tour now, whatever was seen before. */
    replay: () => void;
    /** Turn tours that start by themselves on or off. Rejects when not saved. */
    setEnabled: (on: boolean) => Promise<void>;
    /** Forget every tour seen, so each page's tour runs again. Rejects when not saved. */
    resetAll: () => Promise<void>;
}

const OFF: ToursContextValue = {
    available: false,
    loaded: false,
    enabled: false,
    pageHasTour: false,
    replay: () => {},
    setEnabled: async () => {},
    resetAll: async () => {},
};

export const ToursContext = createContext<ToursContextValue>(OFF);

export function useTours(): ToursContextValue {
    return useContext(ToursContext);
}
