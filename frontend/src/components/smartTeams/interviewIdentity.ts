/** Keep delayed interview work tied to the identity that started it. */
export interface InterviewTimerScheduler {
    setTimeout(callback: () => void, delayMs: number): number;
    clearTimeout(timer: number): void;
}

export function createInterviewIdentityGate(
    identityIsCurrent: () => boolean,
    scheduler: InterviewTimerScheduler,
) {
    let epoch = 0;
    let active = true;
    const timers = new Set<number>();

    const gate = {
        capture: () => epoch,
        isCurrent: (captured: number) => active && captured === epoch && identityIsCurrent(),
        schedule(captured: number, callback: () => void, delayMs: number): number | null {
            if (!gate.isCurrent(captured)) return null;
            const timer = scheduler.setTimeout(() => {
                timers.delete(timer);
                if (gate.isCurrent(captured)) callback();
            }, delayMs);
            timers.add(timer);
            return timer;
        },
        cancel(timer: number | null): void {
            if (timer === null) return;
            scheduler.clearTimeout(timer);
            timers.delete(timer);
        },
        invalidate(): void {
            active = false;
            epoch += 1;
            for (const timer of timers) scheduler.clearTimeout(timer);
            timers.clear();
        },
        activate(): void {
            active = true;
        },
    };
    return gate;
}
